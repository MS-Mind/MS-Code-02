"""Callbacks for mindspore.Model"""
import os
from time import time

# import stat
import numpy as np

import mindspore as ms
from mindspore import ParameterTuple, SummaryRecord, Tensor, load_param_into_net
from mindspore import log as logger
from mindspore import ops, save_checkpoint
from mindspore.train.callback import Callback

from .checkpoint_manager import CheckpointManager
from .reduce_manager import AllReduceSum

__all__ = [
    "StateMonitor",
    "ValCallback",
]


class StateMonitor(Callback):
    """
    Train loss and validation accuracy monitor, after each epoch save the
    best checkpoint file with highest validation accuracy.
    """

    def __init__(
        self,
        model,
        summary_dir="./",
        dataset_val=None,
        val_interval=1,
        val_start_epoch=1,
        save_best_ckpt=True,
        ckpt_dir="./",
        ckpt_save_interval=1,
        best_ckpt_name="best.ckpt",
        metric_name=["accuracy"],
        rank_id=None,
        device_num=None,
        log_interval=100,
        model_name="",
        last_epoch=0,
        keep_checkpoint_max=10,
        ckpt_save_policy=None,
        ema=False,
        dataset_sink_mode=True,
    ):
        super().__init__()
        self.model = model
        self.dataset_val = dataset_val
        self.val_start_epoch = val_start_epoch
        self.save_best_ckpt = save_best_ckpt
        self.metric_name = metric_name
        self.best_res = 0
        self.val_interval = val_interval
        self.summary_dir = summary_dir
        self.rank_id = rank_id if rank_id is not None else 0
        self.device_num = device_num if rank_id is not None else 1
        self.log_interval = log_interval
        self.model_name = model_name
        self.ckpt_dir = ckpt_dir
        self.ckpt_save_interval = ckpt_save_interval
        self.last_epoch = last_epoch
        self.best_epoch = -1

        self.keep_checkpoint_max = keep_checkpoint_max
        self.ckpt_save_policy = ckpt_save_policy
        self._manager = CheckpointManager(ckpt_save_policy=self.ckpt_save_policy)
        self._need_flush_from_cache = True
        self.dataset_sink_mode = dataset_sink_mode

        if self.rank_id in [0, None]:
            if not os.path.isdir(ckpt_dir):
                os.makedirs(ckpt_dir)
            self.log_txt_fp = os.path.join(ckpt_dir, "result.log")
            result_log = "Epoch\tTrainLoss\t"
            name_dict = {"Top_1_Accuracy": "ValAcc@1", "Top_5_Accuracy": "ValAcc@5"}
            for i in range(len(self.metric_name)):
                if self.metric_name[i] in name_dict.keys():
                    result_log += name_dict[self.metric_name[i]] + "\t"
                else:
                    result_log += self.metric_name[i] + "\t"
            result_log += "Time\n"
            with open(self.log_txt_fp, "w", encoding="utf-8") as fp:
                fp.write(result_log)

            self.best_ckpt_path = os.path.join(ckpt_dir, best_ckpt_name)

        if self.device_num > 1:
            self.all_reduce = AllReduceSum()

        self.start = time()
        self.epoch_start = time()
        self.map = ops.HyperMap()
        self.ema = ema
        if self.ema:
            self.online_params = ParameterTuple(self.model.train_network.get_parameters())
            self.swap_params = self.online_params.clone("swap", "zeros")

    def __enter__(self):
        self.summary_record = SummaryRecord(self.summary_dir)
        return self

    def __exit__(self, *exc_args):
        self.summary_record.close()

    def apply_eval(self, run_context):
        """Model evaluation, return validation accuracy."""
        if self.ema:
            cb_params = run_context.original_args()
            self.map(ops.assign, self.swap_params, self.online_params)
            ema_dict = dict()
            if self.dataset_sink_mode:
                net = cb_params.train_network.network
            else:
                net = cb_params.train_network
            for param in net.get_parameters():
                if param.name.startswith("ema"):
                    new_name = param.name.split("ema.")[1]
                    ema_dict[new_name] = param.data
            load_param_into_net(self.model.train_network.network, ema_dict)
            res = self.model.eval(self.dataset_val, dataset_sink_mode=False)
            self.map(ops.assign, self.online_params, self.swap_params)
        else:
            res = self.model.eval(self.dataset_val, dataset_sink_mode=False)

        return res

    def on_train_step_end(self, run_context):
        cb_params = run_context.original_args()
        num_batches = cb_params.batch_num
        cur_epoch = cb_params.cur_epoch_num + self.last_epoch - 1  # (global_step-1) // num_batches
        cur_step_in_epoch = int((cb_params.cur_step_num - 1) % cb_params.batch_num)

        if cb_params.optimizer is not None:
            optimizer = cb_params.optimizer
        elif self.dataset_sink_mode:
            optimizer = cb_params.train_network.network.optimizer
        else:
            optimizer = cb_params.train_network.optimizer

        if (
            (cur_step_in_epoch + 1) % self.log_interval == 0
            or (cur_step_in_epoch + 1) >= num_batches
            or cur_step_in_epoch == 0
        ):
            step = optimizer.global_step
            if optimizer.dynamic_lr:
                cur_lr = optimizer.learning_rate(step - 1)[0].asnumpy()
            else:
                cur_lr = optimizer.learning_rate.asnumpy()
            loss = self._get_loss(cb_params)

            print(
                f"Epoch: {cur_epoch+1}, "
                f"batch:[{cur_step_in_epoch+1}/{num_batches}], "
                f"loss:{loss.asnumpy():.6f}, lr: {cur_lr:.7f},  time:{time() - self.start:.6f}s"
            )
            self.start = time()

    def on_train_epoch_end(self, run_context):
        """
        After epoch, print train loss and val accuracy,
        save the best ckpt file with highest validation accuracy.
        """
        cb_params = run_context.original_args()
        if cb_params.optimizer is not None:
            optimizer = cb_params.optimizer
        elif self.dataset_sink_mode:
            optimizer = cb_params.train_network.network.optimizer
        else:
            optimizer = cb_params.train_network.optimizer

        # the global step may larger than batch_size * epoch due to graph mode async
        global_step = optimizer.global_step.asnumpy()[0]
        cur_epoch = cb_params.cur_epoch_num + self.last_epoch
        cur_step_in_epoch = cb_params.batch_num  # (global_step - 1) % cb_params.batch_num

        loss = self._get_loss(cb_params)
        self.summary_record.add_value("scalar", f"train_loss_{self.rank_id}", loss)

        # val while training if validation loader is not None
        res = Tensor(np.zeros(len(self.metric_name)), ms.float32)
        if self.dataset_val is not None:
            if cur_epoch >= self.val_start_epoch and (cur_epoch - self.val_start_epoch) % self.val_interval == 0:
                val_time = time()
                mind_res = self.apply_eval(run_context)
                for i in range(len(self.metric_name)):
                    res[i] = mind_res[self.metric_name[i]] * 100

                if self.device_num > 1:
                    res = self.all_reduce(res)
                    res /= self.device_num
                # record val acc
                if self.rank_id in [0, None]:
                    metric_str = "Validation "
                    for i in range(len(self.metric_name)):
                        metric_str += self.metric_name[i] + ": " + str(res[i]) + ", "
                    metric_str += f"time:{time() -val_time:.6f}s"
                    print(metric_str)
                    # Save the best ckpt file
                    if res[0] > self.best_res:
                        self.best_res = res[0]
                        self.best_epoch = cur_epoch
                        if self.save_best_ckpt and (self.rank_id == 0):
                            save_checkpoint(cb_params.train_network, self.best_ckpt_path, async_save=True)
                            print(f"=> New best val acc: {res[0].asnumpy():.3f}")

                    if not isinstance(res, Tensor):
                        res = Tensor(res)
                    for i in range(len(res)):
                        self.summary_record.add_value("scalar", "val_" + self.metric_name[i], res[i])

        # log
        if self.rank_id in [0, None]:
            if (cur_epoch % self.ckpt_save_interval == 0) or (cur_epoch == cb_params.epoch_num):
                if self._need_flush_from_cache:
                    self._flush_from_cache(cb_params)

                # save optim for resume
                optim_save_path = os.path.join(self.ckpt_dir, f"optim_{self.model_name}.ckpt")
                ms.save_checkpoint(optimizer, optim_save_path, async_save=True)

                cur_ckpoint_file = self.model_name + "-" + str(cur_epoch) + "_" + str(cur_step_in_epoch) + ".ckpt"

                # keep checkpoint files number equal max number.
                ckpt_save_path = os.path.join(self.ckpt_dir, cur_ckpoint_file)
                ckpoint_filelist = self._manager.save_ckpoint(
                    cb_params.train_network,
                    num_ckpt=self.keep_checkpoint_max,
                    metric=res[0],
                    save_path=ckpt_save_path,
                )
                if self.ckpt_save_policy == "top_k":
                    print("Top K accuracy checkpoints:")
                    print("\n".join(ckpt + "\t" + str(acc) for ckpt, acc in ckpoint_filelist))
                else:
                    print(f"Saving model to {ckpt_save_path}")

            epoch_time = time() - self.epoch_start
            print(f"Total time since last epoch: {epoch_time:.3f}")
            print("-" * 80)
            self.epoch_start = time()
            result_log = f"{cur_epoch}\t\t\t{loss.asnumpy():.7f}\t\t\t"
            for i in range(len(res)):
                result_log += f"{res[i].asnumpy():.3f}\t\t\t"
            result_log += f"{epoch_time:.2f}\n"
            with open(self.log_txt_fp, "a", encoding="utf-8") as fp:
                fp.write(result_log)

        self.summary_record.record(int(global_step))

    # pylint: disable=unused-argument
    def on_train_end(self, run_context):
        if self.dataset_val is not None and self.rank_id == 0:
            print("Finish training!")
            print(f"The best validation {self.metric_name[0]} is: {self.best_res} at epoch {self.best_epoch}.")
        print("=" * 80)

    def _get_loss(self, cb_params):
        """
        Get loss from the network output.
        Args:
            cb_params (_InternalCallbackParam): Callback parameters.
        Returns:
            Union[Tensor, None], if parse loss success, will return a Tensor value(shape is [1]), else return None.
        """
        output = cb_params.net_outputs
        if output is None:
            logger.warning("Can not find any output by this network, so SummaryCollector will not collect loss.")
            return None

        if isinstance(output, (int, float, Tensor)):
            loss = output
        elif isinstance(output, (list, tuple)) and output:
            # If the output is a list, since the default network returns loss first,
            # we assume that the first one is loss.
            loss = output[0]
        else:
            logger.warning(
                "The output type could not be identified, expect type is one of "
                "[int, float, Tensor, list, tuple], so no loss was recorded in SummaryCollector."
            )
            return None

        if not isinstance(loss, Tensor):
            loss = Tensor(loss)

        loss = Tensor(np.mean(loss.asnumpy()))
        return loss

    def _flush_from_cache(self, cb_params):
        """Flush cache data to host if tensor is cache enable."""
        has_cache_params = False
        params = cb_params.train_network.get_parameters()
        for param in params:
            if param.cache_enable:
                has_cache_params = True
                Tensor(param).flush_from_cache()
        if not has_cache_params:
            self._need_flush_from_cache = False

    def remove_oldest_ckpoint_file(self):
        """Remove the oldest checkpoint file from this checkpoint manager and also from the directory."""
        ckpoint_files = sorted(self._ckpoint_filelist, key=os.path.getmtime)
        self.remove_ckpoint_file(ckpoint_files[0])


class ValCallback(Callback):
    def __init__(self, log_step_interval=100):
        super().__init__()
        self.log_step_interval = log_step_interval

    def on_eval_step_end(self, run_context):
        cb_params = run_context.original_args()
        # cur_step_in_epoch = int((cb_params.cur_step_num - 1) % cb_params.batch_num)
        if cb_params.cur_step_num % self.log_step_interval == 0:
            print(f"{cb_params.cur_step_num }/{cb_params.batch_num}")
