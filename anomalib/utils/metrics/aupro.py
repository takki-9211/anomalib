"""Implementation of AUPRO score based on TorchMetrics."""
from typing import Any, Callable, List, Optional, Tuple

import torch
from kornia.contrib import connected_components
from matplotlib.figure import Figure
from torch import Tensor
from torchmetrics import Metric
from torchmetrics.functional import auc, roc
from torchmetrics.utilities.data import dim_zero_cat

from .plotting_utils import plot_figure


class AUPRO(Metric):
    """Area under per region overlap (AUPRO) Metric."""

    is_differentiable: bool = False
    higher_is_better: Optional[bool] = None
    full_state_update: bool = False
    preds: List[Tensor]
    target: List[Tensor]

    def __init__(
        self,
        compute_on_step: bool = True,
        dist_sync_on_step: bool = False,
        process_group: Optional[Any] = None,
        dist_sync_fn: Callable = None,
        fpr_limit: float = 0.3,
    ) -> None:
        super().__init__(
            compute_on_step=compute_on_step,
            dist_sync_on_step=dist_sync_on_step,
            process_group=process_group,
            dist_sync_fn=dist_sync_fn,
        )

        self.add_state("preds", default=[], dist_reduce_fx="cat")  # pylint: disable=not-callable
        self.add_state("target", default=[], dist_reduce_fx="cat")  # pylint: disable=not-callable
        self.fpr_limit = fpr_limit

    def update(self, preds: Tensor, target: Tensor) -> None:  # type: ignore
        """Update state with new values.

        Args:
            preds (Tensor): predictions of the model
            target (Tensor): ground truth targets
        """
        self.target.append(target)
        self.preds.append(preds)

    def _compute(self) -> Tuple[Tensor, Tensor]:
        """Compute the pro/fpr value-pairs until the fpr specified by self.fpr_limit.

        It leverages the fact that the overlap corresponds to the tpr, and thus computes the overall
        PRO curve by aggregating per-region tpr/fpr values produced by ROC-construction.

        Raises:
            ValueError: ValueError is raised if self.target doesn't conform with requirements imposed by kornia for
                        connected component analysis.

        Returns:
            Tuple[Tensor, Tensor]: tuple containing final fpr and tpr values.
        """
        target = dim_zero_cat(self.target)
        preds = dim_zero_cat(self.preds)

        # check and prepare target for labeling via kornia
        if target.min() < 0 or target.max() > 1:
            raise ValueError(
                (
                    f"kornia.contrib.connected_components expects input to lie in the interval [0, 1], but found "
                    f"interval was [{target.min()}, {target.max()}]."
                )
            )
        target = target.unsqueeze(1)  # kornia expects N1HW format
        target = target.type(torch.float)  # kornia expects FloatTensor
        cca = connected_components(target)

        preds = preds.flatten()
        cca = cca.flatten()
        target = target.flatten()

        # compute the global fpr-size
        fpr: Tensor = roc(preds, target)[0]  # only need fpr
        output_size = torch.where(fpr <= self.fpr_limit)[0].size(0)

        # compute the PRO curve by aggregating per-region tpr/fpr curves/values.
        tpr = torch.zeros(output_size, device=preds.device, dtype=torch.float)
        fpr = torch.zeros(output_size, device=preds.device, dtype=torch.float)
        new_idx = torch.arange(0, output_size, device=preds.device)

        # Loop over the labels, computing per-region tpr/fpr curves, and aggregating them.
        # Note that, since the groundtruth is different for every all to `roc`, we also get
        # different/unique tpr/fpr curves (i.e. len(_fpr_idx) is different for every call).
        # We therefore need to resample per-region curves to a fixed sampling ratio (defined above).
        labels = cca.unique()[1:]  # 0 is background
        _fpr: Tensor
        _tpr: Tensor
        for label in labels:
            mask = cca == label
            _fpr, _tpr = roc(preds, mask)[:-1]  # don't need threshs
            _fpr_idx = torch.where(_fpr <= self.fpr_limit)[0]
            _fpr = _fpr[_fpr_idx]
            _tpr = _tpr[_fpr_idx]
            _fpr_idx = _fpr_idx.float()
            _fpr_idx /= _fpr_idx.max()
            _fpr_idx *= new_idx.max()
            _tpr = self.interp1d(_fpr_idx, _tpr, new_idx)
            _fpr = self.interp1d(_fpr_idx, _fpr, new_idx)
            tpr += _tpr
            fpr += _fpr

        # Actually perform the averaging
        tpr /= labels.size(0)
        fpr /= labels.size(0)
        return fpr, tpr

    def compute(self) -> Tensor:
        """Fist compute PRO curve, then compute and scale area under the curve.

        Returns:
            Tensor: Value of the AUPRO metric
        """
        fpr, tpr = self._compute()

        aupro = auc(fpr, tpr)
        aupro = aupro / fpr[-1]  # normalize the area

        return aupro

    def generate_figure(self) -> Tuple[Figure, str]:
        """Generate a figure containing the PRO curve and the AUPRO.

        Returns:
            Tuple[Figure, str]: Tuple containing both the figure and the figure title to be used for logging
        """
        fpr, tpr = self._compute()
        aupro = self.compute()

        xlim = (0.0, self.fpr_limit)
        ylim = (0.0, 1.0)
        xlabel = "Global FPR"
        ylabel = "Averaged Per-Region TPR"
        loc = "lower right"
        title = "PRO"

        fig, _axis = plot_figure(fpr, tpr, aupro, xlim, ylim, xlabel, ylabel, loc, title)

        return fig, "PRO"

    @staticmethod
    def interp1d(old_x: Tensor, old_y: Tensor, new_x: Tensor) -> Tensor:
        """Function to interpolate a 1D signal linearly to new sampling points.

        Args:
            old_x (Tensor): original 1-D x values (same size as y)
            old_y (Tensor): original 1-D y values (same size as x)
            new_x (Tensor): x-values where y should be interpolated at

        Returns:
            Tensor: y-values at corresponding new_x values.
        """

        # Compute slope
        eps = torch.finfo(old_y.dtype).eps
        slope = (old_y[1:] - old_y[:-1]) / (eps + (old_x[1:] - old_x[:-1]))

        # Prepare idx for linear interpolation
        idx = torch.searchsorted(old_x, new_x)

        # searchsorted looks for the index where the values must be inserted
        # to preserve order, but we actually want the preceeding index.
        idx -= 1
        # we clamp the index, because the number of intervals = old_x.size(0) -1,
        # and the left neighbour should hence be at most number of intervals -1, i.e. old_x.size(0) - 2
        idx = torch.clamp(idx, 0, old_x.size(0) - 2)

        # perform actual linear interpolation
        y_new = old_y[idx] + slope[idx] * (new_x - old_x[idx])

        return y_new
