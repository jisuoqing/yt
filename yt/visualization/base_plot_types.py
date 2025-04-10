import sys
import warnings
from abc import ABC
from io import BytesIO
from typing import TYPE_CHECKING, Optional, TypedDict

import matplotlib
import numpy as np
from matplotlib.scale import SymmetricalLogTransform
from matplotlib.ticker import LogFormatterMathtext

from yt._typing import AlphaT
from yt.funcs import (
    get_interactivity,
    is_sequence,
    matplotlib_style_context,
    mylog,
    setdefault_mpl_metadata,
    setdefaultattr,
)
from yt.visualization._handlers import ColorbarHandler, NormHandler

from ._commons import (
    get_canvas,
    validate_image_name,
)

if matplotlib.__version_info__ >= (3, 8):
    from matplotlib.ticker import SymmetricalLogLocator
else:
    from ._commons import _MPL38_SymmetricalLogLocator as SymmetricalLogLocator

if TYPE_CHECKING:
    from typing import Literal

    from matplotlib.axes import Axes
    from matplotlib.axis import Axis
    from matplotlib.figure import Figure
    from matplotlib.transforms import Transform

    class FormatKwargs(TypedDict):
        style: Literal["scientific"]
        scilimits: tuple[int, int]
        useMathText: bool


BACKEND_SPECS = {
    "macosx": ["backend_macosx", "FigureCanvasMac", "FigureManagerMac"],
    "qt5agg": ["backend_qt5agg", "FigureCanvasQTAgg", None],
    "qtagg": ["backend_qtagg", "FigureCanvasQTAgg", None],
    "tkagg": ["backend_tkagg", "FigureCanvasTkAgg", None],
    "wx": ["backend_wx", "FigureCanvasWx", None],
    "wxagg": ["backend_wxagg", "FigureCanvasWxAgg", None],
    "gtk3cairo": [
        "backend_gtk3cairo",
        "FigureCanvasGTK3Cairo",
        "FigureManagerGTK3Cairo",
    ],
    "gtk3agg": ["backend_gtk3agg", "FigureCanvasGTK3Agg", "FigureManagerGTK3Agg"],
    "webagg": ["backend_webagg", "FigureCanvasWebAgg", None],
    "nbagg": ["backend_nbagg", "FigureCanvasNbAgg", "FigureManagerNbAgg"],
    "agg": ["backend_agg", "FigureCanvasAgg", None],
}


class CallbackWrapper:
    def __init__(self, viewer, window_plot, frb, field, font_properties, font_color):
        self.frb = frb
        self.data = frb.data_source
        self._axes = window_plot.axes
        self._figure = window_plot.figure
        if len(self._axes.images) > 0:
            self.raw_image_shape = self._axes.images[0]._A.shape
            if viewer._has_swapped_axes:
                # store the original un-transposed shape
                self.raw_image_shape = self.raw_image_shape[1], self.raw_image_shape[0]
        if frb.axis is not None:
            DD = frb.ds.domain_width
            xax = frb.ds.coordinates.x_axis[frb.axis]
            yax = frb.ds.coordinates.y_axis[frb.axis]
            self._period = (DD[xax], DD[yax])
        self.ds = frb.ds
        self.xlim = viewer.xlim
        self.ylim = viewer.ylim
        self._swap_axes = viewer._has_swapped_axes
        self._flip_horizontal = viewer._flip_horizontal  # needed for quiver
        self._flip_vertical = viewer._flip_vertical  # needed for quiver
        # an important note on _swap_axes: _swap_axes will swap x,y arguments
        # in callbacks (e.g., plt.plot(x,y) will be plt.plot(y, x). The xlim
        # and ylim arguments above, and internal callback references to coordinates
        # are the **unswapped** ranges.
        self._axes_unit_names = viewer._axes_unit_names
        if "OffAxisSlice" in viewer._plot_type:
            self._type_name = "CuttingPlane"
        else:
            self._type_name = viewer._plot_type
        self.aspect = window_plot._aspect
        self.font_properties = font_properties
        self.font_color = font_color
        self.field = field
        self._transform = viewer._transform


class PlotMPL:
    """A base class for all yt plots made using matplotlib, that is backend independent."""

    def __init__(
        self,
        fsize,
        axrect: tuple[float, float, float, float],
        *,
        norm_handler: NormHandler,
        figure: Optional["Figure"] = None,
        axes: Optional["Axes"] = None,
    ):
        """Initialize PlotMPL class"""
        import matplotlib.figure

        self._plot_valid = True
        if figure is None:
            if not is_sequence(fsize):
                fsize = (fsize, fsize)
            self.figure = matplotlib.figure.Figure(figsize=fsize, frameon=True)
        else:
            figure.set_size_inches(fsize)
            self.figure = figure
        if axes is None:
            self._create_axes(axrect)
        else:
            axes.clear()
            axes.set_position(axrect)
            self.axes = axes
        self.interactivity = get_interactivity()

        figure_canvas, figure_manager = self._get_canvas_classes()
        self.canvas = figure_canvas(self.figure)
        if figure_manager is not None:
            # with matplotlib >= 3.9, figure_manager should always be not None
            # see _get_canvas_classes for details.
            self.manager = figure_manager(self.canvas, 1)

        self.axes.tick_params(
            which="both", axis="both", direction="in", top=True, right=True
        )

        self.norm_handler = norm_handler

    def _create_axes(self, axrect: tuple[float, float, float, float]) -> None:
        self.axes = self.figure.add_axes(axrect)

    def _get_canvas_classes(self):
        if self.interactivity:
            key = str(matplotlib.get_backend())
        else:
            key = "agg"

        if matplotlib.__version_info__ >= (3, 9):
            # once yt has a minimum matplotlib version of 3.9, this branch
            # can replace the rest of this function and BACKEND_SPECS can
            # be removed. See https://github.com/yt-project/yt/issues/5138
            from matplotlib.backends import backend_registry

            mod = backend_registry.load_backend_module(key)
            return mod.FigureCanvas, mod.FigureManager

        module, fig_canvas, fig_manager = BACKEND_SPECS[key.lower()]

        mod = __import__(
            "matplotlib.backends",
            globals(),
            locals(),
            [module],
            0,
        )
        submod = getattr(mod, module)
        FigureCanvas = getattr(submod, fig_canvas)
        if fig_manager is not None:
            FigureManager = getattr(submod, fig_manager)
            return FigureCanvas, FigureManager

        return FigureCanvas, None

    def save(self, name, mpl_kwargs=None, canvas=None):
        """Choose backend and save image to disk"""

        if mpl_kwargs is None:
            mpl_kwargs = {}

        name = validate_image_name(name)
        setdefault_mpl_metadata(mpl_kwargs, name)

        try:
            canvas = get_canvas(self.figure, name)
        except ValueError:
            canvas = self.canvas

        mylog.info("Saving plot %s", name)
        with matplotlib_style_context():
            canvas.print_figure(name, **mpl_kwargs)
        return name

    def show(self):
        try:
            self.manager.show()
        except AttributeError:
            self.canvas.show()

    def _get_labels(self):
        ax = self.axes
        labels = ax.xaxis.get_ticklabels() + ax.yaxis.get_ticklabels()
        labels += ax.xaxis.get_minorticklabels()
        labels += ax.yaxis.get_minorticklabels()
        labels += [
            ax.title,
            ax.xaxis.label,
            ax.yaxis.label,
            ax.xaxis.get_offset_text(),
            ax.yaxis.get_offset_text(),
        ]
        return labels

    def _set_font_properties(self, font_properties, font_color):
        for label in self._get_labels():
            label.set_fontproperties(font_properties)
            if font_color is not None:
                label.set_color(font_color)

    def _repr_png_(self):
        from matplotlib.backends.backend_agg import FigureCanvasAgg

        canvas = FigureCanvasAgg(self.figure)
        f = BytesIO()
        with matplotlib_style_context():
            canvas.print_figure(f)
        f.seek(0)
        return f.read()


class ImagePlotMPL(PlotMPL, ABC):
    """A base class for yt plots made using imshow"""

    _default_font_size = 18.0

    def __init__(
        self,
        fsize=None,
        axrect=None,
        caxrect=None,
        *,
        norm_handler: NormHandler,
        colorbar_handler: ColorbarHandler,
        figure: Optional["Figure"] = None,
        axes: Optional["Axes"] = None,
        cax: Optional["Axes"] = None,
    ):
        """Initialize ImagePlotMPL class object"""

        self._transform: Transform | None
        setdefaultattr(self, "_transform", None)

        self.colorbar_handler = colorbar_handler
        _missing_layout_specs = [_ is None for _ in (fsize, axrect, caxrect)]

        if all(_missing_layout_specs):
            fsize, axrect, caxrect = self._get_best_layout()
        elif any(_missing_layout_specs):
            raise TypeError(
                "ImagePlotMPL cannot be initialized with partially specified layout."
            )

        super().__init__(
            fsize, axrect, norm_handler=norm_handler, figure=figure, axes=axes
        )

        if cax is None:
            self.cax = self.figure.add_axes(caxrect)
        else:
            cax.clear()
            cax.set_position(caxrect)
            self.cax = cax

    def _setup_layout_constraints(
        self, figure_size: tuple[float, float] | float, fontsize: float
    ):
        # Setup base layout attributes
        # derived classes need to call this before super().__init__
        # but they are free to do other stuff in between

        if isinstance(figure_size, tuple):
            assert len(figure_size) == 2
            assert all(isinstance(_, float) for _ in figure_size)
            self._figure_size = figure_size
        else:
            assert isinstance(figure_size, float)
            self._figure_size = (figure_size, figure_size)

        self._draw_axes = True
        fontscale = float(fontsize) / self.__class__._default_font_size
        if fontscale < 1.0:
            fontscale = np.sqrt(fontscale)

        self._cb_size = 0.0375 * self._figure_size[0]
        self._ax_text_size = [1.2 * fontscale, 0.9 * fontscale]
        self._top_buff_size = 0.30 * fontscale
        self._aspect = 1.0

    def _reset_layout(self) -> None:
        size, axrect, caxrect = self._get_best_layout()
        self.axes.set_position(axrect)
        self.cax.set_position(caxrect)
        self.figure.set_size_inches(*size)

    def _init_image(self, data, extent, aspect, *, alpha: AlphaT = None):
        """Store output of imshow in image variable"""

        norm = self.norm_handler.get_norm(data)
        extent = [float(e) for e in extent]

        if self._transform is None:
            # sets the transform to be an ax.TransData object, where the
            # coordinate system of the data is controlled by the xlim and ylim
            # of the data.
            transform = self.axes.transData
        else:
            transform = self._transform

        self._validate_axes_extent(extent, transform)

        self.image = self.axes.imshow(
            data.to_ndarray(),
            origin="lower",
            extent=extent,
            norm=norm,
            aspect=aspect,
            cmap=self.colorbar_handler.cmap,
            interpolation="nearest",
            interpolation_stage="data",
            transform=transform,
            alpha=alpha,
        )
        self._set_axes()

    def _set_axes(self) -> None:
        fmt_kwargs: FormatKwargs = {
            "style": "scientific",
            "scilimits": (-2, 3),
            "useMathText": True,
        }
        self.image.axes.ticklabel_format(**fmt_kwargs)
        self.image.axes.set_facecolor(self.colorbar_handler.background_color)

        self.cax.tick_params(which="both", direction="in")

        # For creating a multipanel plot by ImageGrid
        # we may need the location keyword, which requires Matplotlib >= 3.7.0
        cb_location = getattr(self.cax, "orientation", None)
        if matplotlib.__version_info__ >= (3, 7):
            self.cb = self.figure.colorbar(self.image, self.cax, location=cb_location)
        else:
            if cb_location in ["top", "bottom"]:
                warnings.warn(
                    "Cannot properly set the orientation of colorbar. "
                    "Consider upgrading matplotlib to version 3.7 or newer",
                    stacklevel=6,
                )
            self.cb = self.figure.colorbar(self.image, self.cax)

        cb_axis: Axis
        if self.cb.orientation == "vertical":
            cb_axis = self.cb.ax.yaxis
        else:
            cb_axis = self.cb.ax.xaxis

        cb_scale = cb_axis.get_scale()
        if cb_scale == "symlog":
            trf = cb_axis.get_transform()
            if not isinstance(trf, SymmetricalLogTransform):
                raise RuntimeError
            cb_axis.set_major_locator(SymmetricalLogLocator(trf))
            cb_axis.set_major_formatter(
                LogFormatterMathtext(linthresh=trf.linthresh, base=trf.base)
            )

        if cb_scale not in ("log", "symlog"):
            self.cb.ax.ticklabel_format(**fmt_kwargs)

        if self.colorbar_handler.draw_minorticks and cb_scale == "symlog":
            # no minor ticks are drawn by default in symlog, as of matplotlib 3.7.1
            # see https://github.com/matplotlib/matplotlib/issues/25994
            trf = cb_axis.get_transform()
            if not isinstance(trf, SymmetricalLogTransform):
                raise RuntimeError
            if float(trf.base).is_integer():
                locator = SymmetricalLogLocator(trf, subs=list(range(1, int(trf.base))))
                cb_axis.set_minor_locator(locator)
        elif self.colorbar_handler.draw_minorticks:
            self.cb.minorticks_on()
        else:
            self.cb.minorticks_off()

    def _validate_axes_extent(self, extent, transform):
        # if the axes are cartopy GeoAxes, this checks that the axes extent
        # is properly set.

        if "cartopy" not in sys.modules:
            # cartopy isn't already loaded, nothing to do here
            return

        from cartopy.mpl.geoaxes import GeoAxes

        if isinstance(self.axes, GeoAxes):
            # some projections have trouble when passing extents at or near the
            # limits. So we only set_extent when the plot is a subset of the
            # globe, within the tolerance of the transform.

            # note that `set_extent` here is setting the extent of the axes.
            # still need to pass the extent arg to imshow in order to
            # ensure that it is properly scaled. also note that set_extent
            # expects values in the coordinates of the transform: it will
            # calculate the coordinates in the projection.
            global_extent = transform.x_limits + transform.y_limits
            thresh = transform.threshold
            if all(
                abs(extent[ie]) < (abs(global_extent[ie]) - thresh) for ie in range(4)
            ):
                self.axes.set_extent(extent, crs=transform)

    def _get_best_layout(self):
        # this method is called in ImagePlotMPL.__init__
        # required attributes
        # - self._figure_size: Union[float, Tuple[float, float]]
        # - self._aspect: float
        # - self._ax_text_size: Tuple[float, float]
        # - self._draw_axes: bool
        # - self.colorbar_handler: ColorbarHandler

        # optional attributes
        # - self._unit_aspect: float

        # Ensure the figure size along the long axis is always equal to _figure_size
        unit_aspect = getattr(self, "_unit_aspect", 1)
        if is_sequence(self._figure_size):
            x_fig_size, y_fig_size = self._figure_size
            y_fig_size *= unit_aspect
        else:
            x_fig_size = y_fig_size = self._figure_size
            scaling = self._aspect / unit_aspect
            if scaling < 1:
                x_fig_size *= scaling
            else:
                y_fig_size /= scaling

        if self.colorbar_handler.draw_cbar:
            cb_size = self._cb_size
            cb_text_size = self._ax_text_size[1] + 0.45
        else:
            cb_size = x_fig_size * 0.04
            cb_text_size = 0.0

        if self._draw_axes:
            x_axis_size = self._ax_text_size[0]
            y_axis_size = self._ax_text_size[1]
        else:
            x_axis_size = x_fig_size * 0.04
            y_axis_size = y_fig_size * 0.04

        top_buff_size = self._top_buff_size

        if not self._draw_axes and not self.colorbar_handler.draw_cbar:
            x_axis_size = 0.0
            y_axis_size = 0.0
            cb_size = 0.0
            cb_text_size = 0.0
            top_buff_size = 0.0

        xbins = np.array([x_axis_size, x_fig_size, cb_size, cb_text_size])
        ybins = np.array([y_axis_size, y_fig_size, top_buff_size])

        size = [xbins.sum(), ybins.sum()]

        x_frac_widths = xbins / size[0]
        y_frac_widths = ybins / size[1]

        # axrect is the rectangle defining the area of the
        # axis object of the plot.  Its range goes from 0 to 1 in
        # x and y directions.  The first two values are the x,y
        # start values of the axis object (lower left corner), and the
        # second two values are the size of the axis object.  To get
        # the upper right corner, add the first x,y to the second x,y.
        axrect = (
            x_frac_widths[0],
            y_frac_widths[0],
            x_frac_widths[1],
            y_frac_widths[1],
        )

        # caxrect is the rectangle defining the area of the colorbar
        # axis object of the plot.  It is defined just as the axrect
        # tuple is.
        caxrect = (
            x_frac_widths[0] + x_frac_widths[1],
            y_frac_widths[0],
            x_frac_widths[2],
            y_frac_widths[1],
        )

        return size, axrect, caxrect

    def _toggle_axes(self, choice, draw_frame=None):
        """
        Turn on/off displaying the axis ticks and labels for a plot.

        Parameters
        ----------
        choice : boolean
            If True, set the axes to be drawn. If False, set the axes to not be
            drawn.
        """
        self._draw_axes = choice
        self._draw_frame = draw_frame
        if draw_frame is None:
            draw_frame = choice
        if self.colorbar_handler.has_background_color and not draw_frame:
            # workaround matplotlib's behaviour
            # last checked with Matplotlib 3.5
            warnings.warn(
                f"Previously set background color {self.colorbar_handler.background_color} "
                "has no effect. Pass `draw_frame=True` if you wish to preserve background color.",
                stacklevel=4,
            )
        self.axes.set_frame_on(draw_frame)
        self.axes.get_xaxis().set_visible(choice)
        self.axes.get_yaxis().set_visible(choice)
        self._reset_layout()

    def _toggle_colorbar(self, choice: bool):
        """
        Turn on/off displaying the colorbar for a plot

        choice = True or False
        """
        self.colorbar_handler.draw_cbar = choice
        self.cax.set_visible(choice)
        size, axrect, caxrect = self._get_best_layout()
        self.axes.set_position(axrect)
        self.cax.set_position(caxrect)
        self.figure.set_size_inches(*size)

    def _get_labels(self):
        labels = super()._get_labels()
        if getattr(self.cb, "orientation", "vertical") == "horizontal":
            cbaxis = self.cb.ax.xaxis
        else:
            cbaxis = self.cb.ax.yaxis
        labels += cbaxis.get_ticklabels()
        labels += [cbaxis.label, cbaxis.get_offset_text()]
        return labels

    def hide_axes(self, *, draw_frame=None):
        """
        Hide the axes for a plot including ticks and labels
        """
        self._toggle_axes(False, draw_frame)
        return self

    def show_axes(self):
        """
        Show the axes for a plot including ticks and labels
        """
        self._toggle_axes(True)
        return self

    def hide_colorbar(self):
        """
        Hide the colorbar for a plot including ticks and labels
        """
        self._toggle_colorbar(False)
        return self

    def show_colorbar(self):
        """
        Show the colorbar for a plot including ticks and labels
        """
        self._toggle_colorbar(True)
        return self


def get_multi_plot(nx, ny, colorbar="vertical", bw=4, dpi=300, cbar_padding=0.4):
    r"""Construct a multiple axes plot object, with or without a colorbar, into
    which multiple plots may be inserted.

    This will create a set of :class:`matplotlib.axes.Axes`, all lined up into
    a grid, which are then returned to the user and which can be used to plot
    multiple plots on a single figure.

    Parameters
    ----------
    nx : int
        Number of axes to create along the x-direction
    ny : int
        Number of axes to create along the y-direction
    colorbar : {'vertical', 'horizontal', None}, optional
        Should Axes objects for colorbars be allocated, and if so, should they
        correspond to the horizontal or vertical set of axes?
    bw : number
        The base height/width of an axes object inside the figure, in inches
    dpi : number
        The dots per inch fed into the Figure instantiation

    Returns
    -------
    fig : :class:`matplotlib.figure.Figure`
        The figure created inside which the axes reside
    tr : list of list of :class:`matplotlib.axes.Axes` objects
        This is a list, where the inner list is along the x-axis and the outer
        is along the y-axis
    cbars : list of :class:`matplotlib.axes.Axes` objects
        Each of these is an axes onto which a colorbar can be placed.

    Notes
    -----
    This is a simple implementation for a common use case.  Viewing the source
    can be instructive, and is encouraged to see how to generate more
    complicated or more specific sets of multiplots for your own purposes.
    """
    import matplotlib.figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    hf, wf = 1.0 / ny, 1.0 / nx
    fudge_x = fudge_y = 1.0
    if colorbar is None:
        fudge_x = fudge_y = 1.0
    elif colorbar.lower() == "vertical":
        fudge_x = nx / (cbar_padding + nx)
        fudge_y = 1.0
    elif colorbar.lower() == "horizontal":
        fudge_x = 1.0
        fudge_y = ny / (cbar_padding + ny)
    fig = matplotlib.figure.Figure((bw * nx / fudge_x, bw * ny / fudge_y), dpi=dpi)

    fig.set_canvas(FigureCanvasAgg(fig))
    fig.subplots_adjust(
        wspace=0.0, hspace=0.0, top=1.0, bottom=0.0, left=0.0, right=1.0
    )
    tr = []
    for j in range(ny):
        tr.append([])
        for i in range(nx):
            left = i * wf * fudge_x
            bottom = fudge_y * (1.0 - (j + 1) * hf) + (1.0 - fudge_y)
            ax = fig.add_axes([left, bottom, wf * fudge_x, hf * fudge_y])
            tr[-1].append(ax)
    cbars = []
    if colorbar is None:
        pass
    elif colorbar.lower() == "horizontal":
        for i in range(nx):
            # left, bottom, width, height
            # Here we want 0.10 on each side of the colorbar
            # We want it to be 0.05 tall
            # And we want a buffer of 0.15
            ax = fig.add_axes(
                [
                    wf * (i + 0.10) * fudge_x,
                    hf * fudge_y * 0.20,
                    wf * (1 - 0.20) * fudge_x,
                    hf * fudge_y * 0.05,
                ]
            )
            cbars.append(ax)
    elif colorbar.lower() == "vertical":
        for j in range(ny):
            ax = fig.add_axes(
                [
                    wf * (nx + 0.05) * fudge_x,
                    hf * fudge_y * (ny - (j + 0.95)),
                    wf * fudge_x * 0.05,
                    hf * fudge_y * 0.90,
                ]
            )
            ax.clear()
            cbars.append(ax)
    return fig, tr, cbars
