"""Shared, dependency-light styling for publication-oriented analysis figures."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, ParamSpec, TypeVar


PUBLICATION_RASTER_DPI = 600
PUBLICATION_SINGLE_FIGSIZE = (7.2, 4.6)
PUBLICATION_WIDE_FIGSIZE = (9.2, 4.8)
PUBLICATION_STACKED_FIGSIZE = (8.4, 6.4)

OKABE_ITO_ON_WHITE = (
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#000000",  # black
)
SERIES_MARKERS = ("o", "s", "^", "D", "v", "P", "X")
SERIES_LINESTYLES = ("-", "--", "-.", ":")

_PUBLICATION_RC = {
    "figure.dpi": 125,
    "savefig.dpi": PUBLICATION_RASTER_DPI,
    "savefig.facecolor": "white",
    "savefig.transparent": False,
    "font.family": "DejaVu Sans",
    "font.size": 8.5,
    "axes.titlesize": 10.0,
    "axes.labelsize": 9.0,
    "axes.linewidth": 0.8,
    "axes.formatter.use_mathtext": True,
    "axes.formatter.limits": (-3, 4),
    "xtick.labelsize": 8.0,
    "ytick.labelsize": 8.0,
    "xtick.direction": "in",
    "ytick.direction": "in",
    "xtick.major.size": 4.0,
    "ytick.major.size": 4.0,
    "xtick.minor.size": 2.5,
    "ytick.minor.size": 2.5,
    "legend.fontsize": 7.5,
    "legend.title_fontsize": 8.0,
    "legend.frameon": False,
    "lines.linewidth": 1.4,
    "lines.markersize": 4.5,
    "errorbar.capsize": 2.5,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}

_Parameters = ParamSpec("_Parameters")
_Return = TypeVar("_Return")


@contextmanager
def publication_style() -> Iterator[None]:
    """Apply scoped Matplotlib defaults without changing the user's global style."""

    try:
        import matplotlib as mpl
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    with mpl.rc_context(_PUBLICATION_RC):
        yield


def publication_plot(
    function: Callable[_Parameters, _Return],
) -> Callable[_Parameters, _Return]:
    """Run one plotting function inside the scoped publication style."""

    @wraps(function)
    def wrapped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Return:
        with publication_style():
            return function(*args, **kwargs)

    return wrapped


def ordered_series_style(index: int, count: int) -> dict[str, Any]:
    """Return an ordered color plus redundant marker and line encodings."""

    import matplotlib as mpl

    denominator = max(count - 1, 1)
    fraction = index / denominator if count > 1 else 0.35
    color_position = 0.05 + 0.70 * fraction
    return {
        "color": mpl.colormaps["cividis"](color_position),
        "marker": SERIES_MARKERS[index % len(SERIES_MARKERS)],
        "linestyle": SERIES_LINESTYLES[
            (index // len(SERIES_MARKERS)) % len(SERIES_LINESTYLES)
        ],
    }


def style_axis(axis: Any) -> None:
    """Apply restrained grid, ticks, and spines to an existing axis."""

    axis.set_axisbelow(True)
    axis.grid(True, which="major", color="#D9D9D9", linewidth=0.6)
    axis.tick_params(which="both", top=True, right=True)
    for spine in axis.spines.values():
        spine.set_linewidth(0.8)


def outside_legend(axis: Any, *args: Any, **kwargs: Any) -> Any:
    """Place a compact, frameless legend outside the right plot edge."""

    return axis.legend(
        *args,
        loc="upper left",
        bbox_to_anchor=(1.02, 1.0),
        borderaxespad=0.0,
        handlelength=2.4,
        labelspacing=0.4,
        **kwargs,
    )


def save_publication_figure(figure: Any, destination: str | Path) -> Path:
    """Save one opaque figure with publication raster and vector font settings."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        path,
        dpi=PUBLICATION_RASTER_DPI,
        facecolor="white",
        transparent=False,
    )
    return path


def export_publication_figure_set(
    figure: Any,
    destination_stem: str | Path,
    *,
    formats: tuple[str, ...] = ("png", "pdf", "svg"),
) -> tuple[Path, ...]:
    """Export matching high-resolution raster and editable vector figures."""

    stem = Path(destination_stem)
    if stem.suffix:
        raise ValueError("Publication export destination must not have a suffix.")
    return tuple(
        save_publication_figure(figure, stem.with_suffix(f".{format_name}"))
        for format_name in formats
    )
