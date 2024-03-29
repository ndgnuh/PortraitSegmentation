import torch
import pytorch_lightning as pl
from ptrseg.utils import SegmentDataset, DataLoader
from ptrseg.model import FPNDBNet, SegmentLearner
from torchvision import transforms as T
from pytorch_lightning.callbacks import Callback
from os import path

class SaveLatestCallback(Callback):
    def on_train_epoch_end(self, trainer, module):
        trainer.save_checkpoint("latest.ckpt")

class CheckpointCallback(Callback):
    def __init__(self,  model_name, metric_name, dirpath='checkpoints'):
        super().__init__()
        self.model_name = model_name
        self.metric_name = metric_name
        self.dirpath = dirpath
        self.best = 0
        self.metric_basename = path.basename(metric_name)
        self.previous_checkpoint = ""

    def on_validation_epoch_end(self, trainer, module):
        metric = trainer.logged_metrics.get(self.metric_name, 0)
        max_metric = max(metric, self.best)
        if max_metric == metric and self.best != max_metric:
            self.best = max_metric
            if path.isfile(self.previous_checkpoint):
                os.remove(self.previous_checkpoint)
            file = f"{self.metric_basename}={max_metric:.4f}_epoch={trainer.current_epoch:04d}.cpkt"
            file = path.join(self.dirpath, self.model_name, file)
            trainer.save_checkpoint(file)
            self.previous_checkpoint = file


image_size = (640, 640)
transform = T.ColorJitter(0.5, 0.5, 0.5)
train_data = SegmentDataset(root="data/PPM-100/",
                            image_size=image_size,
                            transform=transform,
                            mask_dir="matte")
test_data = SegmentDataset(root="data/PPM-100/",
                           image_size=image_size,
                           transform=transform,
                           mask_dir="matte")
train_loader = DataLoader(train_data, batch_size=4)
val_loader = DataLoader(test_data, batch_size=4)

model = SegmentLearner(hidden_dim=252, learning_rate=5e-4)
model.model.to_onnx("segm.onnx", torch.rand(1, 3, *image_size))
model.eval()
trainer = pl.Trainer(accelerator='cuda',
                     # limit_train_batches=20,
                     # limit_val_batches=5,
                     callbacks=[
                         CheckpointCallback("PtrSgm", "Score 00"),
                         SaveLatestCallback()
                     ],
                     max_epochs=15)
trainer.fit(model, train_loader, val_loader)

trainer.save_checkpoint("latest.ckpt")
model.cpu()
model.eval()
model.model.to_onnx("segm.onnx", torch.rand(1, 3, 640, 640))
