# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Tools to build an ePSF.
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import copy
import warnings

import numpy as np
from astropy.stats import SigmaClip
from astropy.utils.exceptions import AstropyUserWarning

from .epsf_fitter import EPSFFitter
from .models import PSF2DModel


__all__ = ['EPSFBuilder']


def _py2intround(a):
    """
    Round the input to the nearest integer.

    If two integers are equally close, rounding is done away from 0.
    """

    data = np.asanyarray(a)
    value = np.where(data >= 0, np.floor(data + 0.5),
                     np.ceil(data - 0.5)).astype(int)

    if not hasattr(a, '__iter__'):
        value = np.asscalar(value)

    return value


class EPSFBuilder(object):
    """
    Class to build the ePSF.

    Parameters
    ----------
        # NOTE: center_accuracy_sq applies to each star

        pixel_scale : float or tuple of two floats, optional
            The pixel scale (in arbitrary units) of the output PSF.  The
            ``pixel_scale`` can either be a single float or tuple of two
            floats of the form ``(x_pixscale, y_pixscale)``.  If
            ``pixel_scale`` is a scalar then the pixel scale will be the
            same for both the x and y axes.  The PSF ``pixel_scale`` is used
            in conjunction with the star pixel scale when building and
            fitting the PSF.  This allows for building (and fitting) a PSF
            using images of stars with different pixel scales (e.g. velocity
            aberrations).  Either ``oversampling`` or ``pixel_scale`` must
            be input.  If both are input, ``pixel_scale`` will override the
            input ``oversampling``.

        oversampling : float or tuple of two floats, optional
            The oversampling factor(s) of the PSF relative to the input
            ``psf_stars`` along the x and y axes.  The ``oversampling``
            can either be a single float or a tuple of two floats of the
            form ``(x_oversamp, y_oversamp)``.  If ``oversampling`` is a
            scalar then the oversampling will be the same for both the x
            and y axes.  The ``oversampling`` factor will be used with
            the minimum pixel scale of all input PSF stars to calculate
            the PSF pixel scale.  Either ``oversampling`` or
            ``pixel_scale`` must be input.  If both are input,
            ``oversampling`` will be ignored.

        shape : tuple, optional
            The shape of the output PSF.  If the ``shape`` is not input,
            it will be derived from the sizes of the input ``psf_stars``
            and the PSF oversampling factor.  The output PSF will always
            have odd sizes along both axes.


    oversampling : float, optional
        Determines the output ePSF pixel scale, relative to PSFstar cutout
        data.

    smoothing_kernel : {'quartic', 'quadratic'}, 2D `~numpy.ndarray`, or `None`
        The smoothing kernel to apply to the PSF.  The predefined
        kernels ``'quartic'`` and ``'quadratic'`` have been optimized
        for PSF oversampling factors close to 4.  Alternatively, a
        custom 2D array can be input.  If `None` then no smoothing will
        be performed.  The default is ``'quartic'``.
    """

    def __init__(self, pixel_scale=None, oversampling=4., shape=None,
                 peak_fit_box=(5, 5),
                 recenter_accuracy=1.0e-4, recenter_maxiters=1000,
                 smoothing_kernel='quartic',
                 fitter=EPSFFitter(), maxiters=50,
                 center_accuracy=1.0e-4, epsf=None):

        if pixel_scale is None and oversampling is None:
            raise ValueError('Either pixel_scale or oversampling must be '
                             'input.')

        self.pixel_scale = pixel_scale
        self.oversampling = oversampling
        self.shape = shape

        self.peak_fit_box = peak_fit_box

        recenter_accuracy = float(recenter_accuracy)
        if recenter_accuracy <= 0.0:
            raise ValueError('recenter_accuracy must be a strictly positive '
                             'number.')
        self.recenter_accuracy = recenter_accuracy

        recenter_maxiters = int(recenter_maxiters)
        if recenter_maxiters <= 0:
            raise ValueError('recenter_maxiters must be a positive integer.')
        self.recenter_maxiters = recenter_maxiters

        self.smoothing_kernel = smoothing_kernel
        self.fitter = fitter

        maxiters = int(maxiters)
        if maxiters <= 0:
            raise ValueError('maxiters must be a positive number.')
        self.maxiters = maxiters

        if center_accuracy <= 0.0:
            raise ValueError('center_accuracy must be a positive number.')
        self.center_accuracy_sq = center_accuracy**2

        self.epsf = epsf

    def __call__(self, psfstars):
        return self.build_psf(psfstars)

    def _create_initial_psf(self, psf_stars):
        """
        Create an initial `PSF2DModel` object.

        The initial PSF data are all zeros.  The PSF pixel scale is
        determined either from the ``pixel_scale`` or ``oversampling``
        values.

        If ``shape`` is specified, the shape of the PSF data array is
        determined from the shape of the input ``psf_stars`` and the
        oversampling factor (derived from the ratio of the ``psf_star``
        pixel scale to the PSF pixel scale).  The output PSF will always
        have odd sizes along both axes.

        Parameters
        ----------
        psf_stars : `PSFStars` object
            The PSF stars used to build the PSF.

        Returns
        -------
        psf : `PSF2DModel`
            The initial PSF model.
        """

        pixel_scale = self.pixel_scale
        oversampling = self.oversampling
        shape = self.shape

        if pixel_scale is None and oversampling is None:
            raise ValueError('Either pixel_scale or oversampling must be '
                             'input.')

        # define the PSF pixel scale
        if pixel_scale is not None:
            pixel_scale = np.atleast_1d(pixel_scale).astype(float)
            if len(pixel_scale) == 1:
                pixel_scale = np.repeat(pixel_scale, 2)

            oversampling = (psf_stars._min_pixel_scale[0] / pixel_scale[0],
                            psf_stars._min_pixel_scale[1] / pixel_scale[1])
        else:
            oversampling = np.atleast_1d(oversampling).astype(float)
            if len(oversampling) == 1:
                oversampling = np.repeat(oversampling, 2)

            pixel_scale = (psf_stars._min_pixel_scale[0] / oversampling[0],
                           psf_stars._min_pixel_scale[1] / oversampling[1])

        # define the PSF shape
        if shape is not None:
            shape = np.atleast_1d(shape).astype(int)
            if len(shape) == 1:
                shape = np.repeat(shape, 2)
        else:
            x_shape = np.int(np.ceil(psf_stars._max_shape[0] *
                                     oversampling[1]))
            y_shape = np.int(np.ceil(psf_stars._max_shape[1] *
                                     oversampling[0]))
            shape = np.array((y_shape, x_shape))

        shape = [(i + 1) for i in shape if i % 2 == 0]   # ensure odd sizes

        data = np.zeros(shape, dtype=np.float)
        xcenter = (shape[1] - 1) / 2.
        ycenter = (shape[0] - 1) / 2.

        return PSF2DModel(data=data, origin=(xcenter, ycenter),
                          normalize=False, pixel_scale=pixel_scale)

    def _resample_residual(self, psf_star, psf):
        """
        Compute a normalized residual image in the oversampled PSF grid.

        A normalized residual image is calculated by subtracting the
        normalized PSF model from the normalized PSF star at the
        location of the PSF star in the undersampled grid.  The
        normalized residual image is then resampled from the
        undersampled PSF star grid to the oversampled PSF grid.

        Parameters
        ----------
        psf_star : `PSFStar` object
            A single PSF star object.

        psf : `PSF2DModel` object, optional
            The PSF model.

        Returns
        -------
        image : 2D `~numpy.ndarray`
            A 2D image containing the resampled residual image.  The
            image contains NaNs where there is no data.
        """

        # find the integer index of PSFStar pixels in the oversampled
        # PSF grid
        x_oversamp = psf_star.pixel_scale[0] / psf.pixel_scale[0]
        y_oversamp = psf_star.pixel_scale[1] / psf.pixel_scale[1]
        x = x_oversamp * psf_star._xidx_centered
        y = y_oversamp * psf_star._yidx_centered
        psf_xcenter, psf_ycenter = psf.origin
        xidx = _py2intround(x + psf_xcenter)
        yidx = _py2intround(y + psf_ycenter)

        mask = np.logical_and(np.logical_and(xidx >= 0, xidx < psf.shape[1]),
                              np.logical_and(yidx >= 0, yidx < psf.shape[0]))
        xidx = xidx[mask]
        yidx = yidx[mask]

        # Compute the normalized residual image by subtracting the
        # normalized PSF model from the normalized PSF star at the
        # location of the PSF star in the undersampled grid.  Then,
        # resample the normalized residual image in the oversampled
        # PSF grid.
        # [(star - (psf * xov * yov)) / (xov * yov)]
        # --> [(star / (xov * yov)) - psf]
        stardata = ((psf_star._data_values_normalized /
                     (x_oversamp * y_oversamp)) -
                    psf.evaluate(x=x, y=y, flux=1.0, x_0=0.0, y_0=0.0))

        resampled_img = np.full(psf.shape, np.nan)
        resampled_img[yidx, xidx] = stardata[mask]

        return resampled_img

    def _resample_residuals(self, psf_stars, psf):
        """
        Compute normalized residual images for all the input PSF stars.

        Parameters
        ----------
        psf_stars : `PSFStars` object
            The PSF stars used to build the PSF.

        psf : `PSF2DModel` object, optional
            The PSF model.

        Returns
        -------
        star_imgs : 3D `~numpy.ndarray`
            A 3D cube containing the resampled residual images.
        """

        star_imgs = np.zeros((psf_stars.n_good_psfstars, *psf.shape))
        for i, psf_star in enumerate(psf_stars.all_good_psfstars):
            star_imgs[i, :, :] = self._resample_residual(psf_star, psf)

        return star_imgs

    @staticmethod
    def _interpolate_missing_data(data, mask, method='cubic'):
        """
        Interpolate missing data as identified by the ``mask`` keyword.

        Parameters
        ----------
        data : 2D `~numpy.ndarray`
            An array containing the 2D image.

        mask : 2D bool `~numpy.ndarray`
            A 2D booleen mask array with the same shape as the input
            ``data``, where a `True` value indicates the corresponding
            element of ``data`` is masked.  The masked data points are
            those that will be interpolated.

        method : {'cubic', 'nearest'}, optional
            The method of used to "interpolate" the  missing data:

            - ``'cubic'``:  Masked data are interpolated using 2D cubic
              splines.  This is the default.

            - ``'nearest'``:  Masked data are interpolated using
              nearest-neighbor interpolation.

        Returns
        -------
        data_interp : 2D `~numpy.ndarray`
            The interpolated 2D image.
        """

        from scipy import interpolate

        data_interp = np.array(data, copy=True)

        if len(data_interp.shape) != 2:
            raise ValueError('data must be a 2D array.')

        if mask.shape != data.shape:
            raise ValueError('mask and data must have the same shape.')

        y, x = np.indices(data_interp.shape)
        xy = np.dstack((x[~mask].ravel(), y[~mask].ravel()))[0]
        z = data_interp[~mask].ravel()

        if method == 'nearest':
            interpol = interpolate.NearestNDInterpolator(xy, z)
        elif method == 'cubic':
            interpol = interpolate.CloughTocher2DInterpolator(xy, z)
        else:
            raise ValueError('Unsupported interpolation method.')

        xy_missing = np.dstack((x[mask].ravel(), y[mask].ravel()))[0]
        data_interp[mask] = interpol(xy_missing)

        return data_interp

    def _smooth_psf(self, psf_data):
        """
        Smooth the PSF array by convolving it with a kernel.

        Parameters
        ----------
        psf_data : 2D `~numpy.ndarray`
            A 2D array containing the PSF image.

        Returns
        -------
        result : 2D `~numpy.ndarray`
            The smoothed (convolved) PSF data.
        """

        from scipy.ndimage import convolve

        if self.smoothing_kernel == 'quadratic':
            # from Polynomial2D fit with degree=2 to 5x5 array of
            # zeros with 1. at the center
            # Polynomial2D(2, c0_0=-0.07428571, c1_0=0.11428571,
            #              c2_0=-0.02857143, c0_1=0.11428571,
            #              c0_2=-0.02857143, c1_1=-0.)
            kernel = np.array(
                [[-0.07428311, 0.01142786, 0.03999952, 0.01142786,
                  -0.07428311],
                 [+0.01142786, 0.09714283, 0.12571449, 0.09714283,
                  +0.01142786],
                 [+0.03999952, 0.12571449, 0.15428215, 0.12571449,
                  +0.03999952],
                 [+0.01142786, 0.09714283, 0.12571449, 0.09714283,
                  +0.01142786],
                 [-0.07428311, 0.01142786, 0.03999952, 0.01142786,
                  -0.07428311]])

        elif self.smoothing_kernel == 'quartic':
            # from Polynomial2D fit with degree=4 to 5x5 array of
            # zeros with 1. at the center
            # Polynomial2D(4, c0_0=0.04163265, c1_0=-0.76326531,
            #              c2_0=0.99081633, c3_0=-0.4, c4_0=0.05,
            #              c0_1=-0.76326531, c0_2=0.99081633, c0_3=-0.4,
            #              c0_4=0.05, c1_1=0.32653061, c1_2=-0.08163265,
            #              c1_3=0., c2_1=-0.08163265, c2_2=0.02040816,
            #              c3_1=-0.)>
            kernel = np.array(
                [[+0.041632, -0.080816, 0.078368, -0.080816, +0.041632],
                 [-0.080816, -0.019592, 0.200816, -0.019592, -0.080816],
                 [+0.078368, +0.200816, 0.441632, +0.200816, +0.078368],
                 [-0.080816, -0.019592, 0.200816, -0.019592, -0.080816],
                 [+0.041632, -0.080816, 0.078368, -0.080816, +0.041632]])

        elif isinstance(self.smoothing_kernel, np.ndarray):
            kernel = self.kernel

        else:
            raise TypeError("Unsupported kernel.")

        return convolve(psf_data, kernel)

    def _recenter_psf(self, psf_data, psf):
        """
        Recenter the PSF.
        """

        shift_x = 0
        shift_y = 0
        peak_eps_sq = self.recenter_accuracy**2
        eps_sq_prev = None
        y, x = np.indices(psf_data.shape, dtype=np.float)

        ePSF = psf.make_similar_from_data(psf_data)
        ePSF.fill_value = 0.0

        cx, cy = psf.origin
        for iteration in range(self.recenter_maxiters):
            # find peak location:
            peak_x, peak_y = _find_peak(psf_data, xmax=cx, ymax=cy,
                                        peak_fit_box=self.peak_fit_box,
                                        peak_search_box='fitbox',
                                        mask=None)

            dx = cx - peak_x
            dy = cy - peak_y

            eps_sq = dx**2 + dy**2
            if ((eps_sq_prev is not None and eps_sq > eps_sq_prev)
                    or eps_sq < peak_eps_sq):
                break
            eps_sq_prev = eps_sq

            shift_x += dx
            shift_y += dy

            # Resample PSF data to a shifted grid such that the pick of
            # the PSF is at expected position
            psf_data = ePSF.evaluate(x=x, y=y, flux=1.0, x_0=shift_x + cx,
                                     y_0=shift_y + cy)

        # apply final shifts and fill in any missing data
        if shift_x != 0.0 or shift_y != 0.0:
            ePSF.fill_value = np.nan
            psf_data = ePSF.evaluate(x=x, y=y, flux=1.0, x_0=shift_x + cx,
                                     y_0=shift_y + cy)

            # fill in the "holes" (=np.nan) using 0 (no contribution to
            # the flux)
            psf_data[~np.isfinite(psf_data)] = 0.

        return psf_data

    def _build_psf_step(self, psf_stars, psf=None):
        """
        A single iteration of improving a PSF.

        Parameters
        ----------
        psf_stars : `PSFStars` object
            The PSF stars used to build the PSF.

        psf : `PSF2DModel` object, optional
            The initial PSF model.  If not input, then the PSF will be
            built from scratch.

        Returns
        -------
        psf : `PSF2DModel` object
            The improved PSF.
        """

        if len(psf_stars) < 1:
            raise ValueError('psf_stars must contain at least one PSFStar '
                             'or LinkedPSFStar object.')

        if psf is None:
            # create an initial PSF (array of zeros)
            psf = self._create_initial_psf(psf_stars)
        else:
            # improve the input PSF
            psf = copy.deepcopy(psf)

        # compute a 3D stack of 2D residual images
        residuals = self._resample_residuals(psf_stars, psf)

        # compute the sigma-clipped median along the 3D stack
        # TODO: allow custom SigmaClip/statistic class
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            warnings.simplefilter('ignore', category=AstropyUserWarning)
            sigclip = SigmaClip(sigma=3., cenfunc=np.ma.median, iters=10)
            residuals = sigclip(residuals, axis=0)
            residuals = np.ma.median(residuals, axis=0)
            residuals = residuals.filled(np.nan)

        # interpolate any missing data (np.nan)
        mask = ~np.isfinite(residuals)
        if np.any(mask):
            residuals = self._interpolate_missing_data(residuals, mask,
                                                       method='cubic')

            # fill any remaining nans (outer points) with zeros
            residuals[~np.isfinite(residuals)] = 0.

        # add the residuals to the previous PSF image
        new_psf = psf.normalized_data + residuals

        # smooth the PSF
        new_psf = self._smooth_psf(new_psf)

        # recenter the PSF
        new_psf = self._recenter_psf(new_psf, psf)

        norm = np.abs(np.sum(new_psf, dtype=np.float64))
        new_psf /= norm

        # Create ePSF model and return:
        ePSF = psf.make_similar_from_data(new_psf)

        return ePSF

    def build_psf(self, psf_stars, init_psf=None):
        """
        Iteratively build a PSF from star cutouts.

        If the optional ``psf`` is input, then it will be used as the
        initial PSF.

        Parameters
        ----------
        psf_stars : `PSFStars` object
            The PSF stars used to build the PSF.

        init_psf : `PSF2DModel` object, optional
            The initial PSF model.  If not input, then the PSF will be
            built from scratch.

        Returns
        -------
        psf : `PSF2DModel` object
            The constructed PSF.

        fit_psf_stars : `PSFStars` object
            The input PSF stars with updated centers and fluxes derived
            from fitting the output ``psf``.
        """

        iter_num = 0
        center_dist_sq = self.center_accuracy_sq + 1.
        centers = psf_stars.cutout_center
        n_stars = psf_stars.n_psfstars
        fit_failed = np.zeros(n_stars, dtype=bool)
        dx_dy = np.zeros((n_stars, 2), dtype=np.float)
        psf = init_psf

        while (iter_num < self.maxiters and
                np.max(center_dist_sq) >= self.center_accuracy_sq and
                not np.all(fit_failed)):

            iter_num += 1
            print('iter', iter_num)

            # build/improve the PSF
            psf = self._build_psf_step(psf_stars, psf=psf)

            # fit the new PSF to the psf_stars to find improved centers
            psf_stars = self.fitter(psf, psf_stars)

            # find all psf stars where the fit failed
            fit_failed = np.array([psf_star.fit_error_status > 0
                                   for psf_star in psf_stars.all_psfstars])

            # permanently exclude fitting any psf star where the fit
            # fails after 3 iterations
            if iter_num > 3 and np.any(fit_failed):
                idx = fit_failed.nonzero()[0]
                for i in idx:
                    psf_stars.all_psfstars[i]._excluded_from_fit = True

            dx_dy = psf_stars.cutout_center - centers
            dx_dy = dx_dy[np.logical_not(fit_failed)]
            center_dist_sq = np.sum(dx_dy * dx_dy, axis=1, dtype=np.float64)
            centers = psf_stars.cutout_center

        return psf, psf_stars


def _find_peak(image_data, xmax=None, ymax=None, peak_fit_box=5,
               peak_search_box=None, mask=None):
    """
    Find location of the peak in an array. This is done by fitting a second
    degree 2D polynomial to the data within a `peak_fit_box` and computing the
    location of its maximum. When `xmax` and `ymax` are both `None`, an initial
    estimate of the position of the maximum will be performed by searching
    for the location of the pixel/array element with the maximum value. This
    kind of initial brute-force search can be performed even when
    `xmax` and `ymax` are provided but when one suspects that these input
    coordinates may not be very accurate by specifying an expanded
    brute-force search box through parameter `peak_search_box`.

    Parameters
    ----------
    image_data : numpy.ndarray
        Image data.

    xmax : float, None, optional
        Initial guess of the x-coordinate of the peak. When both `xmax` and
        `ymax` are `None`, the initial (pre-fit) estimate of the location
        of the peak will be obtained by a brute-force search for the location
        of the maximum-value pixel in the *entire* `image_data` array,
        regardless of the value of ``peak_search_box`` parameter.

    ymax : float, None, optional
        Initial guess of the x-coordinate of the peak. When both `xmax` and
        `ymax` are `None`, the initial (pre-fit) estimate of the location
        of the peak will be obtained by a brute-force search for the location
        of the maximum-value pixel in the *entire* `image_data` array,
        regardless of the value of ``peak_search_box`` parameter.

    peak_fit_box : int, tuple of int, optional
        Size (in pixels) of the box around the input estimate of the maximum
        (given by ``xmax`` and ``ymax``) to be used for quadratic fitting from
        which peak location is computed. If a single integer
        number is provided, then it is assumed that fitting box is a square
        with sides of length given by ``peak_fit_box``. If a tuple of two
        values is provided, then first value indicates the width of the box and
        the second value indicates the height of the box.

    peak_search_box : str {'all', 'off', 'fitbox'}, int, tuple of int, None,\
optional
        Size (in pixels) of the box around the input estimate of the maximum
        (given by ``xmax`` and ``ymax``) to be used for brute-force search of
        the maximum value pixel. This search is performed before quadratic
        fitting in order to improve the original estimate of the peak
        given by input ``xmax`` and ``ymax``. If a single integer
        number is provided, then it is assumed that search box is a square
        with sides of length given by ``peak_fit_box``. If a tuple of two
        values is provided, then first value indicates the width of the box
        and the second value indicates the height of the box. ``'off'`` or
        `None` turns off brute-force search of the maximum. When
        ``peak_search_box`` is ``'all'`` then the entire ``image_data``
        data array is searched for maximum and when it is set to ``'fitbox'``
        then the brute-force search is performed in the same box as
        ``peak_fit_box``.

        .. note::
            This parameter is ignored when both `xmax` and `ymax` are `None`
            since in that case the brute-force search for the maximum is
            performed in the entire input array.

    mask : numpy.ndarray, optional
        A boolean type `~numpy.ndarray` indicating "good" pixels in image data
        (`True`) and "bad" pixels (`False`). If not provided all pixels
        in `image_data` will be used for fitting.

    Returns
    -------
    coord : tuple of float
        A pair of coordinates of the peak.

    """
    # check arguments:
    if ((xmax is None and ymax is not None) or (ymax is None and
                                                xmax is not None)):
        raise ValueError("Both 'xmax' and 'ymax' must be either None or not "
                         "None")

    image_data = np.asarray(image_data, dtype=np.float64)
    ny, nx = image_data.shape

    # process peak search box:
    if peak_search_box == 'fitbox':
        peak_search_box = peak_fit_box

    elif peak_search_box == 'off':
        peak_search_box = None

    elif peak_search_box == 'all':
        peak_search_box = image_data.shape

    if xmax is None:
        # find index of the pixel having maximum value:
        if mask is None:
            jmax, imax = np.unravel_index(np.argmax(image_data),
                                          image_data.shape)
            coord = (float(imax), float(jmax))

        else:
            j, i = np.indices(image_data.shape)
            i = i[mask]
            j = j[mask]
            ind = np.argmax(image_data[mask])
            imax = i[ind]
            jmax = j[ind]
            coord = (float(imax), float(jmax))

        auto_expand_search = False  # we have already searched the data

    else:
        imax = _py2intround(xmax)
        jmax = _py2intround(ymax)
        coord = (xmax, ymax)

        if peak_search_box is not None:
            sbx, sby = _process_box_pars(peak_search_box)

            # choose a box around maxval pixel:
            x1 = max(0, imax - sbx // 2)
            x2 = min(nx, x1 + sbx)
            y1 = max(0, jmax - sby // 2)
            y2 = min(ny, y1 + sby)

            if x1 < x2 and y1 < y2:
                search_cutout = image_data[y1:y2, x1:x2]
                jmax, imax = np.unravel_index(
                    np.argmax(search_cutout),
                    search_cutout.shape
                )
                imax += x1
                jmax += y1
                coord = (float(imax), float(jmax))

        auto_expand_search = (sbx != nx or sby != ny)

    wx, wy = _process_box_pars(peak_fit_box)

    if wx * wy < 6:
        # we need at least 6 points to fit a 2D quadratic polynomial
        return coord

    # choose a box around maxval pixel:
    x1 = max(0, imax - wx // 2)
    x2 = min(nx, x1 + wx)
    y1 = max(0, jmax - wy // 2)
    y2 = min(ny, y1 + wy)

    # if peak is at the edge of the box, return integer indices of the max:
    if imax == x1 or imax == x2 or jmax == y1 or jmax == y2:
        return (float(imax), float(jmax))

    # expand the box if needed:
    if (x2 - x1) < wx:
        if x1 == 0:
            x2 = min(nx, x1 + wx)
        if x2 == nx:
            x1 = max(0, x2 - wx)
    if (y2 - y1) < wy:
        if y1 == 0:
            y2 = min(ny, y1 + wy)
        if y2 == ny:
            y1 = max(0, y2 - wy)

    if (x2 - x1) * (y2 - y1) < 6:
        # we need at least 6 points to fit a 2D quadratic polynomial
        return coord

    # fit a 2D 2nd degree polynomial to data:
    xi = np.arange(x1, x2)
    yi = np.arange(y1, y2)
    x, y = np.meshgrid(xi, yi)
    x = x.ravel()
    y = y.ravel()
    v = np.vstack((np.ones_like(x), x, y, x*y, x*x, y*y)).T
    d = image_data[y1:y2, x1:x2].ravel()
    if mask is not None:
        m = mask[y1:y2, x1:x2].ravel()
        v = v[m]
        d = d[m]
        if d.size < 6:
            # we need at least 6 points to fit a 2D quadratic polynomial
            return coord
    try:
        c = np.linalg.lstsq(v, d, rcond=None)[0]
    except np.linalg.LinAlgError:
        if auto_expand_search:
            return _find_peak(image_data, xmax=None, ymax=None,
                              peak_fit_box=(wx, wy), mask=mask)
        else:
            return coord

    # find maximum of the polynomial:
    _, c10, c01, c11, c20, c02 = c
    d = 4 * c02 * c20 - c11**2
    if d <= 0 or ((c20 > 0.0 and c02 >= 0.0) or (c20 >= 0.0 and c02 > 0.0)):
        # polynomial is does not have max. return middle of the window:
        if auto_expand_search:
            return _find_peak(image_data, xmax=None, ymax=None,
                              peak_fit_box=(wx, wy), mask=mask)
        else:
            return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    xm = (c01 * c11 - 2.0 * c02 * c10) / d
    ym = (c10 * c11 - 2.0 * c01 * c20) / d

    if xm > 0.0 and xm < (nx - 1.0) and ym > 0.0 and ym < (ny - 1.0):
        coord = (xm, ym)
    elif auto_expand_search:
        coord = _find_peak(image_data, xmax=None, ymax=None,
                           peak_fit_box=(wx, wy), mask=mask)

    return coord


def _process_box_pars(par):
    if hasattr(par, '__iter__'):
        if len(par) != 2:
            raise TypeError("Box specification must be either a scalar or "
                            "an iterable with two elements.")
        wx = int(par[0])
        wy = int(par[1])
    else:
        wx = int(par)
        wy = int(par)

    if wx < 1 or wy < 1:
        raise ValueError("Box dimensions must be positive integer numbers.")

    return (wx, wy)
