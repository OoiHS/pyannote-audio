# MIT License
#
# Copyright (c) 2020- CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import math
import random
from typing import Dict, Optional, Sequence, Text, Union

import numpy as np
import torch
import torch.nn.functional
from matplotlib import pyplot as plt
from pyannote.core import Segment, SlidingWindowFeature
from pyannote.database.protocol import SpeakerDiarizationProtocol
from pyannote.database.protocol.protocol import Scope, Subset
from pytorch_lightning.loggers import MLFlowLogger, TensorBoardLogger
from torch_audiomentations import OneOf
from torch_audiomentations.core.transforms_interface import BaseWaveformTransform
from torch_audiomentations.utils.object_dict import ObjectDict
from torchmetrics import Metric

from pyannote.audio.core.task import Problem, Resolution, Specifications, Task
from pyannote.audio.tasks.segmentation.mixins import SegmentationTaskMixin
from pyannote.audio.torchmetrics import (
    DiarizationErrorRate,
    FalseAlarmRate,
    MissedDetectionRate,
    OptimalDiarizationErrorRate,
    OptimalDiarizationErrorRateThreshold,
    OptimalFalseAlarmRate,
    OptimalMissedDetectionRate,
    OptimalSpeakerConfusionRate,
    SpeakerConfusionRate,
)
from pyannote.audio.utils.loss import nll_loss
from pyannote.audio.utils.permutation import permutate
from pyannote.audio.utils.powerset import Powerset

Subsets = list(Subset.__args__)
Scopes = list(Scope.__args__)


class BaseGuide(BaseWaveformTransform):
    """Guide generator for training guided speaker diarization

    This is a hack built on top of torch-audiomentations.
    It will modify the "target" so they can be used as guide.

    Usage
    -----

    >>> strategy = WhateverGuideStrategy()
    >>> guide = strategy(samples, sample_rate=16000, target=target).target
    """

    supported_modes = {"per_example", "per_channel"}

    supports_multichannel = True
    requires_sample_rate = False

    supports_target = True
    requires_target = True

    def __init__(
        self,
        mode: str = "per_example",
        p: float = 0.5,
        p_mode: str = None,
        sample_rate: int = None,
        target_rate: int = None,
        output_type: str = "dict",
    ):
        super().__init__(
            mode=mode,
            p=p,
            p_mode=p_mode,
            sample_rate=sample_rate,
            target_rate=target_rate,
            output_type=output_type,
        )
        self.training = True

    def randomize_parameters(
        self,
        samples: torch.Tensor = None,
        sample_rate: Optional[int] = None,
        targets: Optional[torch.Tensor] = None,
        target_rate: Optional[int] = None,
    ):
        batch_size, num_channels, num_frames, num_speakers = targets.shape
        self.transform_parameters["guided"] = torch.zeros(
            (batch_size, num_channels, num_frames),
            dtype=torch.bool,
            device=targets.device,
        )

    def apply_transform(
        self,
        samples: torch.Tensor = None,
        sample_rate: Optional[int] = None,
        targets: Optional[torch.Tensor] = None,
        target_rate: Optional[int] = None,
    ) -> ObjectDict:
        # convert from target space {0, 1} to guide space {-1, 1}
        guides = 2 * (targets - 0.5)

        # obtain boolean mask indicating which frames are guided
        guided = self.transform_parameters["guided"]

        # mark unguided frames as 0.0
        guides[~guided] = 0.0

        return ObjectDict(
            samples=samples,
            sample_rate=sample_rate,
            targets=guides.type(targets.dtype),
            # targets=guides.type(torch.int8),
            target_rate=target_rate,
        )


class NoGuide(BaseGuide):
    def randomize_parameters(
        self,
        samples: torch.Tensor = None,
        sample_rate: Optional[int] = None,
        targets: Optional[torch.Tensor] = None,
        target_rate: Optional[int] = None,
    ):
        batch_size, num_channels, num_frames, _ = targets.shape
        self.transform_parameters["guided"] = torch.zeros(
            (batch_size, num_channels, num_frames),
            dtype=torch.bool,
            device=targets.device,
        )


class FullGuide(BaseGuide):
    def randomize_parameters(
        self,
        samples: torch.Tensor = None,
        sample_rate: Optional[int] = None,
        targets: Optional[torch.Tensor] = None,
        target_rate: Optional[int] = None,
    ):
        batch_size, num_channels, num_frames, _ = targets.shape
        self.transform_parameters["guided"] = torch.ones(
            (batch_size, num_channels, num_frames),
            dtype=torch.bool,
            device=targets.device,
        )


class FirstHalfGuide(BaseGuide):
    def randomize_parameters(
        self,
        samples: torch.Tensor = None,
        sample_rate: Optional[int] = None,
        targets: Optional[torch.Tensor] = None,
        target_rate: Optional[int] = None,
    ):
        batch_size, num_channels, num_frames, _ = targets.shape
        guided = torch.zeros(
            (batch_size, num_channels, num_frames),
            dtype=torch.bool,
            device=targets.device,
        )
        guided[:, :, : (num_frames // 2)] = 1
        self.transform_parameters["guided"] = guided


class RandomFrameGuide(BaseGuide):
    def __init__(self, p: float = 0.5, min_frames: int = 1, max_frames: int = 10):
        super().__init__(p=p)
        self.min_frames = min_frames
        self.max_frames = max_frames

    def randomize_parameters(
        self,
        samples: torch.Tensor = None,
        sample_rate: Optional[int] = None,
        targets: Optional[torch.Tensor] = None,
        target_rate: Optional[int] = None,
    ):
        batch_size, num_channels, num_frames, _ = targets.shape
        guided = torch.zeros(
            (batch_size, num_channels, num_frames),
            dtype=torch.bool,
            device=targets.device,
        )

        for b in range(batch_size):
            num_guided_frames = random.randint(self.min_frames, self.max_frames)
            guided_frames_idx = random.sample(range(num_frames), num_guided_frames)
            guided[b, :, guided_frames_idx] = 1

        self.transform_parameters["guided"] = guided


class GuidedSpeakerDiarization(SegmentationTaskMixin, Task):
    """Guided speaker diarization

    Parameters
    ----------
    protocol : SpeakerDiarizationProtocol
        pyannote.database protocol
    duration : float, optional
        Chunks duration. Defaults to 10s.
    max_speakers_per_chunk : int, optional
        Maximum number of speakers per chunk. Defaults to 3.
    max_speakers_per_frame : int, optional
        Maximum number of (overlapping) speakers per frame. Defaults to 2.
    balance: str, optional
        When provided, training samples are sampled uniformly with respect to that key.
        For instance, setting `balance` to "database" will make sure that each database
        will be equally represented in the training samples.
    freedom : float, optional
        Controls how much freedom the model is allowed regarding the provided guide.
        0.0 means that the model is forced to follow the guide exactly.
        1.0 means that the model is free to ignore the guide completely.
        Defaults to 0.5.
    batch_size : int, optional
        Number of training samples per batch. Defaults to 32.
    num_workers : int, optional
        Number of workers used for generating training samples.
        Defaults to multiprocessing.cpu_count() // 2.
    pin_memory : bool, optional
        If True, data loaders will copy tensors into CUDA pinned
        memory before returning them. See pytorch documentation
        for more details. Defaults to False.
    augmentation : BaseWaveformTransform, optional
        torch_audiomentations waveform transform, used by dataloader
        during training.
    metric : optional
        Validation metric(s). Can be anything supported by torchmetrics.MetricCollection.
        Defaults to AUROC (area under the ROC curve).
    """

    def __init__(
        self,
        protocol: SpeakerDiarizationProtocol,
        duration: float = 10.0,
        max_speakers_per_chunk: int = 3,
        max_speakers_per_frame: int = 2,
        balance: Text = None,
        freedom: float = 0.5,
        batch_size: int = 32,
        num_workers: int = None,
        pin_memory: bool = False,
        augmentation: BaseWaveformTransform = None,
        metric: Union[Metric, Sequence[Metric], Dict[str, Metric]] = None,
    ):
        super().__init__(
            protocol,
            duration=duration,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            augmentation=augmentation,
            metric=metric,
        )

        if not isinstance(protocol, SpeakerDiarizationProtocol):
            raise ValueError(
                "SpeakerDiarization task requires a SpeakerDiarizationProtocol."
            )

        self.max_speakers_per_chunk = max_speakers_per_chunk
        self.max_speakers_per_frame = max_speakers_per_frame
        self.balance = balance
        self.freedom = freedom

        # 50% of training samples have no guide (p=0.5) for use in autonomous mode
        # 25% of training samples are guided by their first half for use in streaming mode
        # 25% of training samples are guided by random frames for use in interactive mode
        self.guidance = OneOf(
            [
                NoGuide(p=1.0),
                NoGuide(p=1.0),
                FirstHalfGuide(p=1.0),
                RandomFrameGuide(p=1.0, min_frames=1, max_frames=10),
            ],
            p=1.0,
            output_type="dict",
        )

        self.specifications = Specifications(
            problem=Problem.MONO_LABEL_CLASSIFICATION,
            resolution=Resolution.FRAME,
            duration=self.duration,
            classes=[f"speaker#{i+1}" for i in range(self.max_speakers_per_chunk)],
            powerset_max_classes=self.max_speakers_per_frame,
            permutation_invariant=True,
        )

    def setup_loss_func(self):
        self.model.powerset = Powerset(
            len(self.specifications.classes),
            self.specifications.powerset_max_classes,
        )

    def prepare_chunk(self, file_id: int, start_time: float, duration: float):
        """Prepare chunk

        Parameters
        ----------
        file_id : int
            File index
        start_time : float
            Chunk start time
        duration : float
            Chunk duration.

        Returns
        -------
        sample : dict
            Dictionary containing the chunk data with the following keys:
            - `X`: waveform
            - `y`: target as a SlidingWindowFeature instance where y.labels is
                   in meta.scope space.
            - `meta`:
                - `scope`: target scope (0: file, 1: database, 2: global)
                - `database`: database index
                - `file`: file index
        """

        file = self.get_file(file_id)

        # get label scope
        label_scope = Scopes[self.metadata[file_id]["scope"]]
        label_scope_key = f"{label_scope}_label_idx"

        #
        chunk = Segment(start_time, start_time + duration)

        sample = dict()
        sample["X"], _ = self.model.audio.crop(file, chunk, duration=duration)

        # gather all annotations of current file
        annotations = self.annotations[self.annotations["file_id"] == file_id]

        # gather all annotations with non-empty intersection with current chunk
        chunk_annotations = annotations[
            (annotations["start"] < chunk.end) & (annotations["end"] > chunk.start)
        ]

        # discretize chunk annotations at model output resolution
        start = np.maximum(chunk_annotations["start"], chunk.start) - chunk.start
        start_idx = np.floor(start / self.model.example_output.frames.step).astype(
            np.int
        )
        end = np.minimum(chunk_annotations["end"], chunk.end) - chunk.start
        end_idx = np.ceil(end / self.model.example_output.frames.step).astype(np.int)

        # get list and number of labels for current scope
        labels = list(np.unique(chunk_annotations[label_scope_key]))
        num_labels = len(labels)

        if num_labels > self.max_speakers_per_chunk:
            pass

        # initial frame-level targets
        y = np.zeros((self.model.example_output.num_frames, num_labels), dtype=np.uint8)

        # map labels to indices
        mapping = {label: idx for idx, label in enumerate(labels)}

        for start, end, label in zip(
            start_idx, end_idx, chunk_annotations[label_scope_key]
        ):
            mapped_label = mapping[label]
            y[start:end, mapped_label] = 1

        sample["y"] = SlidingWindowFeature(
            y, self.model.example_output.frames, labels=labels
        )

        metadata = self.metadata[file_id]
        sample["meta"] = {key: metadata[key] for key in metadata.dtype.names}
        sample["meta"]["file"] = file_id

        return sample

    def collate_y(self, batch) -> torch.Tensor:
        """

        Parameters
        ----------
        batch : list
            List of samples to collate.
            "y" field is expected to be a SlidingWindowFeature.

        Returns
        -------
        y : torch.Tensor
            Collated target tensor of shape (batch_size, num_frames, self.max_speakers_per_chunk)
            If one chunk has more than `self.max_speakers_per_chunk` speakers, we keep
            the max_speakers_per_chunk most talkative ones. If it has less, we pad with
            zeros (artificial inactive speakers).
        """

        collated_y = []
        for b in batch:
            y = b["y"].data
            num_speakers = len(b["y"].labels)

            # keep only the most talkative speakers
            if num_speakers > self.max_speakers_per_chunk:
                # sort speakers in descending talkativeness order
                indices = np.argsort(-np.sum(y, axis=0), axis=0)
                y = y[:, indices[: self.max_speakers_per_chunk]]

            elif num_speakers < self.max_speakers_per_chunk:
                # create inactive speakers by zero padding
                y = np.pad(
                    y,
                    ((0, 0), (0, self.max_speakers_per_chunk - num_speakers)),
                    mode="constant",
                )

            else:
                # we have exactly the right number of speakers
                pass

            # shuffle speaker indices (to avoid having them sorted in talkativeness decreasing order) as
            # the model might otherwise infer prior probabilities from the order of the speakers. we do
            # not want this information (partly computed from the second half of the chunk) to leak.
            np.random.shuffle(y.T)

            collated_y.append(y)

        return torch.from_numpy(np.stack(collated_y))

    def collate_fn(self, batch, stage="train"):
        # collate X
        collated_X = self.collate_X(batch)

        # collate y
        collated_y = self.collate_y(batch)

        # generate guide
        guide = self.guidance(
            samples=collated_X,
            sample_rate=self.model.hparams.sample_rate,
            targets=collated_y.unsqueeze(1).type(torch.int8),
        ).targets.squeeze(1)

        # collate metadata
        collated_meta = self.collate_meta(batch)

        # apply augmentation (only in "train" stage)
        self.augmentation.train(mode=(stage == "train"))
        augmented = self.augmentation(
            samples=collated_X,
            sample_rate=self.model.hparams.sample_rate,
            targets=collated_y.unsqueeze(1),
        )

        return {
            "X": augmented.samples,
            "y": augmented.targets.squeeze(1),
            "guide": guide,
            "meta": collated_meta,
        }

    def segmentation_loss(
        self,
        permutated_prediction: torch.Tensor,
        target: torch.Tensor,
        weight: torch.Tensor = None,
    ) -> torch.Tensor:
        """Permutation-invariant segmentation loss

        Parameters
        ----------
        permutated_prediction : (batch_size, num_frames, num_classes) torch.Tensor
            Permutated speaker activity predictions.
        target : (batch_size, num_frames, num_speakers) torch.Tensor
            Speaker activity.
        weight : (batch_size, num_frames, 1) torch.Tensor, optional
            Frames weight.

        Returns
        -------
        seg_loss : torch.Tensor
            Permutation-invariant segmentation loss
        """

        return nll_loss(
            permutated_prediction,
            torch.argmax(target, dim=-1),
            weight=weight,
        )

    def training_step(self, batch, batch_idx: int):
        """Compute permutation-invariant segmentation loss

        Parameters
        ----------
        batch : (usually) dict of torch.Tensor
            Current batch.
        batch_idx: int
            Batch index.

        Returns
        -------
        loss : {str: torch.tensor}
            {"loss": loss}
        """

        # target
        target_multilabel = batch["y"]
        # (batch_size, num_frames, num_speakers)

        waveform = batch["X"]
        # (batch_size, num_channels, num_samples)

        # drop samples that contain too many speakers
        num_speakers: torch.Tensor = torch.sum(
            torch.any(target_multilabel, dim=1), dim=1
        )
        keep: torch.Tensor = num_speakers <= self.max_speakers_per_chunk
        target_multilabel = target_multilabel[keep]
        waveform = waveform[keep]

        # corner case
        if not keep.any():
            return {"loss": 0.0}

        guide = batch["guide"]
        # guide[:, f, s] = +1/-1 when speaker s is active/inactive at frame f
        # guide[:, f, s] = 0 when we do not know

        # forward pass
        soft_prediction_powerset = self.model(waveform, guide=guide)

        # permutate target in multilabel space and convert it to powerset space
        hard_prediction_multilabel = self.model.powerset.to_multilabel(
            soft_prediction_powerset
        )
        permutated_target_multilabel, _ = permutate(
            hard_prediction_multilabel, target_multilabel
        )
        permutated_target_powerset = self.model.powerset.to_powerset(
            permutated_target_multilabel.float()
        )

        # compute loss in powerset space (between soft prediction and permutated target)
        seg_loss = self.segmentation_loss(
            soft_prediction_powerset, permutated_target_powerset
        )

        self.model.log(
            "loss/train/segmentation",
            seg_loss,
            on_step=False,
            on_epoch=True,
            prog_bar=False,
            logger=True,
        )

        if torch.any(guide != 0.0):
            # turn {-1, +1} into {0, 1}
            guide_multilabel = (guide + 1) * 0.5

            # weight = 1 when frame is guided, 0 otherwise
            weight = torch.any(guide != 0.0, dim=2, keepdim=True).float()

            # permutate guide in multiabel space and convert it to powerset space
            permutated_guide_multilabel, _ = permutate(
                hard_prediction_multilabel, guide_multilabel, weight=weight
            )
            permutated_guide_powerset = self.model.powerset.to_powerset(
                permutated_guide_multilabel.float()
            )

            # compute loss in powerset space (between soft prediction and permutated guide)
            guide_loss = self.segmentation_loss(
                soft_prediction_powerset,
                permutated_guide_powerset,
                weight=weight,
            )

        else:
            guide_loss = 0.0

        self.model.log(
            "loss/train/guide",
            guide_loss,
            on_step=False,
            on_epoch=True,
            prog_bar=False,
            logger=True,
        )

        loss = self.freedom * seg_loss + (1 - self.freedom) * guide_loss

        self.model.log(
            "loss/train",
            loss,
            on_step=False,
            on_epoch=True,
            prog_bar=False,
            logger=True,
        )

        return {"loss": loss}

    def default_metric(
        self,
    ) -> Union[Metric, Sequence[Metric], Dict[str, Metric]]:
        """Returns diarization error rate and its components"""

        if self.specifications.powerset:
            return {
                "DiarizationErrorRate": DiarizationErrorRate(0.5),
                "DiarizationErrorRate/Confusion": SpeakerConfusionRate(0.5),
                "DiarizationErrorRate/Miss": MissedDetectionRate(0.5),
                "DiarizationErrorRate/FalseAlarm": FalseAlarmRate(0.5),
            }

        return {
            "DiarizationErrorRate": OptimalDiarizationErrorRate(),
            "DiarizationErrorRate/Threshold": OptimalDiarizationErrorRateThreshold(),
            "DiarizationErrorRate/Confusion": OptimalSpeakerConfusionRate(),
            "DiarizationErrorRate/Miss": OptimalMissedDetectionRate(),
            "DiarizationErrorRate/FalseAlarm": OptimalFalseAlarmRate(),
        }

    # TODO: no need to compute gradient in this method
    def validation_step(self, batch, batch_idx: int):
        """Compute validation loss and metric

        Parameters
        ----------
        batch : dict of torch.Tensor
            Current batch.
        batch_idx: int
            Batch index.
        """

        # target
        target = batch["y"]
        # (batch_size, num_frames, num_speakers)

        waveform = batch["X"]
        # (batch_size, num_channels, num_samples)

        # TODO: should we handle validation samples with too many speakers
        # waveform = waveform[keep]
        # target = target[keep]

        # forward pass
        prediction = self.model(waveform)
        batch_size, num_frames, _ = prediction.shape

        multilabel = self.model.powerset.to_multilabel(prediction)
        permutated_target, _ = permutate(multilabel, target)

        # FIXME: handle case where target have too many speakers?
        # since we don't need
        permutated_target_powerset = self.model.powerset.to_powerset(
            permutated_target.float()
        )
        seg_loss = self.segmentation_loss(prediction, permutated_target_powerset)

        self.model.log(
            "loss/val/segmentation",
            seg_loss,
            on_step=False,
            on_epoch=True,
            prog_bar=False,
            logger=True,
        )

        self.model.validation_metric(
            torch.transpose(multilabel, 1, 2),
            torch.transpose(target, 1, 2),
        )

        self.model.log_dict(
            self.model.validation_metric,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )

        # log first batch visualization every 2^n epochs.
        if (
            self.model.current_epoch == 0
            or math.log2(self.model.current_epoch) % 1 > 0
            or batch_idx > 0
        ):
            return

        # visualize first 9 validation samples of first batch in Tensorboard/MLflow

        y = permutated_target.float().cpu().numpy()
        y_pred = multilabel.cpu().numpy()

        # prepare 3 x 3 grid (or smaller if batch size is smaller)
        num_samples = min(self.batch_size, 9)
        nrows = math.ceil(math.sqrt(num_samples))
        ncols = math.ceil(num_samples / nrows)
        fig, axes = plt.subplots(
            nrows=2 * nrows, ncols=ncols, figsize=(8, 5), squeeze=False
        )

        # reshape target so that there is one line per class when plotting it
        y[y == 0] = np.NaN
        if len(y.shape) == 2:
            y = y[:, :, np.newaxis]
        y *= np.arange(y.shape[2])

        # plot each sample
        for sample_idx in range(num_samples):
            # find where in the grid it should be plotted
            row_idx = sample_idx // nrows
            col_idx = sample_idx % ncols

            # plot target
            ax_ref = axes[row_idx * 2 + 0, col_idx]
            sample_y = y[sample_idx]
            ax_ref.plot(sample_y)
            ax_ref.set_xlim(0, len(sample_y))
            ax_ref.set_ylim(-1, sample_y.shape[1])
            ax_ref.get_xaxis().set_visible(False)
            ax_ref.get_yaxis().set_visible(False)

            # plot predictions
            ax_hyp = axes[row_idx * 2 + 1, col_idx]
            sample_y_pred = y_pred[sample_idx]
            ax_hyp.plot(sample_y_pred)
            ax_hyp.set_ylim(-0.1, 1.1)
            ax_hyp.set_xlim(0, len(sample_y))
            ax_hyp.get_xaxis().set_visible(False)

        plt.tight_layout()

        for logger in self.model.loggers:
            if isinstance(logger, TensorBoardLogger):
                logger.experiment.add_figure("samples", fig, self.model.current_epoch)
            elif isinstance(logger, MLFlowLogger):
                logger.experiment.log_figure(
                    run_id=logger.run_id,
                    figure=fig,
                    artifact_file=f"samples_epoch{self.model.current_epoch}.png",
                )

        plt.close(fig)
