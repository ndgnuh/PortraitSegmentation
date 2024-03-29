import torch
from torch import nn, Tensor
from torchvision import models
from torchvision.models._utils import IntermediateLayerGetter
from typing import List
import pytorch_lightning as pl
from torchmetrics import JaccardIndex
import numpy as np

configs = dict(
    shufflenet_v2_x2_0=dict(
        features="",
        layers=["stage2", "stage3", "stage4"],
        head_upscale=8,
    ),
    mobilenet_v3_large=dict(
        features="features",
        layers=["3", "6", "12", "16"],
        head_upscale=4,
    )
)


class FeaturePyramidNetwork(nn.Module):
    def __init__(
        self,
        in_channels: List[int],
        out_channels: int,
        deform_conv: bool = False,
    ) -> None:

        super().__init__()

        out_chans = out_channels // len(in_channels)

        conv_layer = DeformConv2d if deform_conv else nn.Conv2d

        self.in_branches = nn.ModuleList(
            [
                nn.Sequential(
                    conv_layer(chans, out_channels, 1, bias=False),
                    nn.BatchNorm2d(out_channels),
                    nn.ReLU(inplace=True),
                )
                for idx, chans in enumerate(in_channels)
            ]
        )
        self.upsample = nn.Upsample(
            scale_factor=2, mode="bilinear", align_corners=True)
        self.out_branches = nn.ModuleList(
            [
                nn.Sequential(
                    conv_layer(out_channels, out_chans,
                               3, padding=1, bias=False),
                    nn.BatchNorm2d(out_chans),
                    nn.ReLU(inplace=True),
                    nn.Upsample(scale_factor=2**idx,
                                mode="bilinear", align_corners=True),
                )
                for idx, chans in enumerate(in_channels)
            ]
        )

    def forward(self, x: List[torch.Tensor]) -> torch.Tensor:
        if len(x) != len(self.out_branches):
            raise AssertionError
        # Conv1x1 to get the same number of channels
        _x: List[torch.Tensor] = [branch(t)
                                  for branch, t in zip(self.in_branches, x)]
        out: List[torch.Tensor] = [_x[-1]]
        for t in _x[:-1][::-1]:
            out.append(self.upsample(out[-1]) + t)

        # Conv and final upsampling
        out = [branch(t) for branch, t in
               zip(self.out_branches, out[::-1])]

        return torch.cat(out, dim=1)


class DBHead(nn.Module):
    def __init__(self, hidden_dim: int):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.thres_head = self.make_branch()
        self.prob_head = self.make_branch()

    def forward(self, x: Tensor) -> Tensor:
        prob = self.prob_head(x)
        thres = self.thres_head(x)
        return prob, thres

    def make_branch(self):
        hidden_dim = self.hidden_dim
        mid_dim = hidden_dim // 4
        # num_layers = np.log(self.upscale) / np.log(2)
        layers = []
        return nn.Sequential(
            nn.ConvTranspose2d(hidden_dim,
                               mid_dim,
                               kernel_size=2,
                               stride=2,
                               bias=False),
            nn.BatchNorm2d(mid_dim),
            nn.ReLU(inplace=True),
            # nn.ConvTranspose2d(mid_dim,
            #                    mid_dim,
            #                    kernel_size=2,
            #                    stride=2,
            #                    bias=False),
            # nn.BatchNorm2d(mid_dim),
            # nn.ReLU(inplace=True),
            nn.ConvTranspose2d(mid_dim, 1, 2, stride=2),
        )


class FPNDBNet(pl.LightningModule):
    def __init__(self, backbone: str, hidden_dim: int = 256):
        super().__init__()

        # config
        cfg = configs[backbone]

        print(cfg)
        backbone = getattr(models, backbone)(pretrained=True)
        self.features = IntermediateLayerGetter(
            dict(backbone.named_modules())[cfg['features']],
            dict(zip(cfg['layers'], cfg['layers']))
        )

        with torch.no_grad():
            features = self.features(torch.rand(1, 3, 112, 112))
        channels = [v.shape[1] for v in features.values()]
        self.fpn_keys = cfg['layers']
        self.fpn = FeaturePyramidNetwork(channels, hidden_dim)
        self.dbhead = DBHead(hidden_dim)

    def forward(self, x: Tensor) -> Tensor:
        features = self.features(x)
        # for k, v in features.items():
        #     print(k, v.shape)
        features = self.fpn([features[k] for k in self.fpn_keys])
        # print('ft', features.shape)
        prob, thres = self.dbhead(features)
        return prob, thres


# LEARNER

class Learner(pl.LightningModule):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.save_hyperparameters()

    def training_step(self, batch):
        output = self(batch)
        return output['loss']

    def training_epoch_end(self, outputs):
        losses = [output['loss'].detach().item() for output in outputs]
        mean_loss = sum(losses) / len(losses)
        self.log("Train loss", mean_loss)

    def validation_step(self, batch, idx):
        output = self(batch)
        scores = output.get('scores', tuple())
        if not isinstance(scores, tuple):
            scores = (scores,)

        if len(scores) == 0:
            print("Warning: score is empty")
        return scores

    def validation_epoch_end(self, outputs):
        n = len(outputs)
        names = self.get_score_names()
        scores = dict()

        # Calculate total score
        for output in outputs:
            for (name, score) in zip(names, output):
                scores[name] = scores.get(name, 0) + score

        # Divide by batch
        for (name, score) in scores.items():
            scores[name] = score.detach().item() / n

        # Best scores
        best_scores = getattr(self, "best_scores", dict())
        for name, score in scores.items():
            best_name = f"Best {name}"
            best = best_scores.get(best_name, 0)
            if score > best:
                best_scores[best_name] = score
        self.best_scores = best_scores

        # log
        self.log_dict(scores)
        self.log_dict(best_scores)

    def get_score_names(self):
        return tuple(f"Score {i:02d}" for i in range(10))

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.learning_rate)


class SegmentLearner(Learner):
    def __init__(self,
                 backbone: str = "mobilenet_v3_large",
                 hidden_dim: int = 255,
                 learning_rate=5e-4):
        super().__init__()
        self.learning_rate = learning_rate
        self.model = FPNDBNet(backbone, hidden_dim)
        self.loss = nn.CrossEntropyLoss(
            weight=torch.tensor([0.1, 1]))
        self.score = JaccardIndex(2)

    def forward(self, batch):
        image, mask = batch
        prob, thres = self.model(image)

        output = dict()
        with torch.no_grad():
            if mask is not None:
                predict = torch.sigmoid(prob) > -0.5
                score = self.score(predict, mask)
                output['scores'] = (score,)

        if self.training:
            logits = torch.cat([thres, prob], dim=1)
            loss = self.loss(logits, mask)
            output['loss'] = loss

        return output
