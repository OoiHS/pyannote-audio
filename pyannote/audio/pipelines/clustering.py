# The MIT License (MIT)
#
# Copyright (c) 2021 CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Clustering pipelines"""


from enum import Enum

import numpy as np
from sklearn.cluster import DBSCAN as SKLearnDBSCAN
from sklearn.cluster import OPTICS as SKLearnOPTICS
from sklearn.cluster import AffinityPropagation as SKLearnAffinityPropagation
from sklearn.cluster import AgglomerativeClustering as SKLearnAgglomerativeClustering
from spectralcluster import (
    AutoTune,
    EigenGapType,
    LaplacianType,
    RefinementName,
    RefinementOptions,
    SpectralClusterer,
    SymmetrizeType,
    ThresholdType,
)

from pyannote.pipeline import Pipeline
from pyannote.pipeline.parameter import Categorical, Integer, Uniform


class AffinityPropagation(Pipeline):
    def __init__(self):
        super().__init__()
        self.damping = Uniform(0.5, 1.0)
        self.preference = Uniform(-50.0, 0.0)  # check what this interval should be

    def initialize(self):
        self._clustering = SKLearnAffinityPropagation(
            damping=self.damping,
            max_iter=200,
            convergence_iter=15,
            copy=True,
            preference=self.preference,
            affinity="precomputed",
            verbose=False,
            random_state=1337,  # for reproducibility
        )

    def __call__(self, affinity: np.ndarray) -> np.ndarray:
        return self._clustering.fit_predict(affinity)


class DBSCAN(Pipeline):
    def __init__(self):
        super().__init__()
        self.eps = Uniform(0.0, 1.0)
        self.min_samples = Integer(2, 100)

    def initialize(self):
        self._clustering = SKLearnDBSCAN(
            eps=self.eps,
            min_samples=self.min_samples,
            metric="precomputed",
            algorithm="auto",
            leaf_size=30,
            n_jobs=None,
        )

    def __call__(self, affinity: np.ndarray) -> np.ndarray:
        return self._clustering.fit_predict(np.clip(1.0 - affinity, 0.0, 1.0))


class OPTICS(Pipeline):
    def __init__(self):
        super().__init__()
        self.min_samples = Integer(2, 100)
        self.max_eps = Uniform(0.0, 1.0)
        self.xi = Uniform(0.0, 1.0)

    def initialize(self):
        self._clustering = SKLearnOPTICS(
            min_samples=self.min_samples,
            max_eps=self.max_eps,
            metric="precomputed",
            cluster_method="xi",
            xi=self.xi,
            predecessor_correction=True,
            min_cluster_size=None,
            algorithm="auto",
            leaf_size=30,
            memory=None,
            n_jobs=None,
        )

    def __call__(self, affinity: np.ndarray) -> np.ndarray:
        return self._clustering.fit_predict(np.clip(1.0 - affinity, 0.0, 1.0))


class AgglomerativeClustering(Pipeline):
    def __init__(self):
        super().__init__()
        self.linkage = Categorical(["complete", "average", "single"])
        self.distance_threshold = Uniform(0.0, 1.0)

    def initialize(self):
        self._clustering = SKLearnAgglomerativeClustering(
            n_clusters=None,
            affinity="precomputed",
            linkage=self.linkage,
            distance_threshold=self.distance_threshold,
        )

    def __call__(self, affinity: np.ndarray) -> np.ndarray:
        return self._clustering.fit_predict(1.0 - affinity)


class SpectralClustering(Pipeline):
    def __init__(self):
        super().__init__()
        self.autotune = Categorical([True, False])
        self.laplacian = Categorical(
            ["Affinity", "Unnormalized", "RandomWalk", "GraphCut"]
        )

    def initialize(self):

        autotune = None
        refinement_options = None

        if self.autotune:
            autotune = AutoTune(
                p_percentile_min=0.50,
                p_percentile_max=0.95,
                init_search_step=0.01,
                search_level=1,
            )

            refinement_options = RefinementOptions(
                thresholding_soft_multiplier=0.01,
                thresholding_type=ThresholdType.Percentile,
                thresholding_with_binarization=True,
                thresholding_preserve_diagonal=True,
                symmetrize_type=SymmetrizeType.Average,
                refinement_sequence=[
                    RefinementName.RowWiseThreshold,
                    RefinementName.Symmetrize,
                ],
            )

        self._clustering = SpectralClusterer(
            min_clusters=None,
            max_clusters=None,
            refinement_options=refinement_options,
            autotune=autotune,
            laplacian_type=LaplacianType[self.laplacian],
            stop_eigenvalue=1e-2,
            row_wise_renorm=False,
            custom_dist="cosine",
            max_iter=300,
            constraint_options=None,
            eigengap_type=EigenGapType.Ratio,
            affinity_function=lambda precomputed: precomputed,  # precomputed affinity
        )

    def __call__(self, affinity: np.ndarray) -> np.ndarray:
        return self._clustering.predict(affinity)


class Clustering(Enum):
    AffinityPropagation = AffinityPropagation
    DBSCAN = DBSCAN
    OPTICS = OPTICS
    AgglomerativeClustering = AgglomerativeClustering
    SpectralClustering = SpectralClustering
