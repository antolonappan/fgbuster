# FGBuster
# Copyright (C) 2019 Davide Poletti, Josquin Errard and the FGBuster developers
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

""" High-level component separation routines

"""
from six import string_types
import logging
import numpy as np
from scipy.optimize import OptimizeResult
import healpy as hp
from . import algebra as alg
from .mixingmatrix import MixingMatrix


__all__ = [
    'basic_comp_sep',
    'weighted_comp_sep',
    'ilc',
    'harmonic_ilc',
    'multi_res_comp_sep',
]


def weighted_comp_sep(components, instrument, data, cov, nside=0,
                      **minimize_kwargs):
    """ Weighted component separation

    Parameters
    ----------
    components: list or tuple of lists
        List storing the :class:`Component` s of the mixing matrix
    instrument:
        Instrument used to define the mixing matrix.
        It can be any object that has what follows either as a key or as an
        attribute (e.g. `dict`, `PySM.Instrument`)

        - **Frequencies**

    data: ndarray or MaskedArray
        Data vector to be separated. Shape *(n_freq, ..., n_pix)*. *...* can be
        also absent.
        Values equal to `hp.UNSEEN` or, if `MaskedArray`, masked values are
        neglected during the component separation process.
    cov: ndarray or MaskedArray
        Covariance maps. It has to be broadcastable to *data*.
        Notice that you can not pass a pixel independent covariance as an array
        with shape *(n_freq,)*: it has to be *(n_freq, ..., 1)* in order to be
        broadcastable (consider using :func:`basic_comp_sep`, in this case).
        Values equal to `hp.UNSEEN` or, if `MaskedArray`, masked values are
        neglected during the component separation process.
    nside:
        For each pixel of a HEALPix map with this nside, the non-linear
        parameters are estimated independently
    patch_ids: array
        For each pixel, the array stores the id of the region over which to
        perform component separation independently.

    Returns
    -------
    result: dict
	It includes

	- **param**: *(list)* - Names of the parameters fitted
	- **x**: *(ndarray)* - ``x[i]`` is the best-fit (map of) the *i*-th
          parameter
        - **Sigma**: *(ndarray)* - ``Sigma[i, j]`` is the (map of) the
          semi-analytic covariance between the *i*-th and the *j*-th parameter
          It is meaningful only in the high signal-to-noise regime and when the
          *cov* is the true covariance of the data
        - **s**: *(ndarray)* - Component amplitude maps
        - **mask_good**: *(ndarray)* - mask of the entries actually used in the
          component separation

    Note
    ----
    During the component separation, a pixel is masked if at least one of
    its frequencies is masked, either in *data* or in *cov*.

    """
    instrument = _force_keys_as_attributes(instrument)
    # Make sure that cov has the frequency dimension and is equal to n_freq
    cov_shape = list(np.broadcast(cov, data).shape)
    if cov.ndim < 2 or (data.ndim == 3 and cov.shape[-2] == 1):
        cov_shape[-2] = 1
    cov = np.broadcast_to(cov, cov_shape, subok=True)
    
    # Prepare mask and set to zero all the frequencies in the masked pixels: 
    # NOTE: mask are good pixels
    mask = ~(_intersect_mask(data) | _intersect_mask(cov))

    invN = np.zeros(cov.shape[:1] + cov.shape)
    for i in range(cov.shape[0]):
        invN[i, i] = 1. / cov[i]
    invN = invN.T
    if invN.shape[0] != 1:
        invN = invN[mask] 
        
    data_cs = hp.pixelfunc.ma_to_array(data).T[mask]
    assert not np.any(hp.ma(data_cs).mask)

    A_ev, A_dB_ev, comp_of_param, x0, params = _A_evaluator(components,
                                                            instrument)
    if not len(x0):
        A_ev = A_ev()

    # Component separation
    if nside:
        patch_ids = hp.ud_grade(np.arange(hp.nside2npix(nside)),
                                hp.npix2nside(data.shape[-1]))[mask]
        res = alg.multi_comp_sep(A_ev, data_cs, invN, A_dB_ev, comp_of_param,
                             patch_ids, x0, **minimize_kwargs)
    else:
        res = alg.comp_sep(A_ev, data_cs, invN, A_dB_ev, comp_of_param, x0,
                       **minimize_kwargs)

    # Craft output
    res.params = params

    def craft_maps(maps):
        # Unfold the masked maps
        # Restore the ordering of the input data (pixel dimension last)
        result = np.full(data.shape[-1:] + maps.shape[1:], hp.UNSEEN)
        result[mask] = maps
        return result.T

    def craft_params(par_array):
        # Add possible last pixels lost due to masking
        # Restore the ordering of the input data (pixel dimension last)
        missing_ids = hp.nside2npix(nside) - par_array.shape[0]
        extra_dims = np.full((missing_ids,) + par_array.shape[1:], hp.UNSEEN)
        result = np.concatenate((par_array, extra_dims))
        result[np.isnan(result)] = hp.UNSEEN
        return result.T

    if len(x0):
        if 'chi_dB' in res:
            res.chi_dB = [craft_maps(c) for c in res.chi_dB]
        if nside:
            res.x = craft_params(res.x)
            res.Sigma = craft_params(res.Sigma)

    res.s = craft_maps(res.s)
    res.chi = craft_maps(res.chi)
    res.invAtNA = craft_maps(res.invAtNA)
    res.mask_good = mask

    return res


def basic_comp_sep(components, instrument, data, nside=0, **minimize_kwargs):
    """ Basic component separation

    Parameters
    ----------
    components: list
        List storing the :class:`Component` s of the mixing matrix
    instrument
        Instrument object used to define the mixing matrix.
        It can be any object that has what follows either as a key or as an
        attribute (e.g. `dict`, `PySM.Instrument`)

        - **Frequencies**
        - **Sens_I** or **Sens_P** (optional, frequencies are inverse-noise
          weighted according to these noise levels)

    data: ndarray or MaskedArray
        Data vector to be separated. Shape *(n_freq, ..., n_pix).*
        *...* can be

        - absent or 1: temperature maps
        - 2: polarization maps
        - 3: temperature and polarization maps (see note)

        Values equal to `hp.UNSEEN` or, if `MaskedArray`, masked values are
        neglected during the component separation process.
    nside:
        For each pixel of a HEALPix map with this nside, the non-linear
        parameters are estimated independently

    Returns
    -------
    result: dict
	It includes

	- **param**: *(list)* - Names of the parameters fitted
	- **x**: *(ndarray)* - ``x[i]`` is the best-fit (map of) the *i*-th
          parameter
        - **Sigma**: *(ndarray)* - ``Sigma[i, j]`` is the (map of) the
          semi-analytic covariance between the *i*-th and the *j*-th parameter.
          It is meaningful only in the high signal-to-noise regime and when the
          *cov* is the true covariance of the data
        - **s**: *(ndarray)* - Component amplitude maps
        - **mask_good**: *(ndarray)* - mask of the entries actually used in the
          component separation

    Note
    ----

    * During the component separation, a pixel is masked if at least one of
      its frequencies is masked.
    * If you provide temperature and polarization maps, they will constrain the
      **same** set of parameters. In particular, separation is **not** done
      independently for temperature and polarization. If you want an
      independent fitting for temperature and polarization, please launch

      >>> res_T = basic_comp_sep(component_T, instrument, data[:, 0], **kwargs)
      >>> res_P = basic_comp_sep(component_P, instrument, data[:, 1:], **kwargs)

    """
    instrument = _force_keys_as_attributes(instrument)
    # Prepare mask and set to zero all the frequencies in the masked pixels:
    # NOTE: mask are bad pixels
    mask = _intersect_mask(data)
    data = hp.pixelfunc.ma_to_array(data).copy()
    data[..., mask] = 0  # Thus no contribution to the spectral likelihood

    try:
        data_nside = hp.get_nside(data[0])
    except TypeError:
        data_nside = 0
    prewhiten_factors = _get_prewhiten_factors(instrument, data.shape,
                                               data_nside)
    A_ev, A_dB_ev, comp_of_param, x0, params = _A_evaluator(
        components, instrument, prewhiten_factors=prewhiten_factors)
    if not len(x0):
        A_ev = A_ev()
    if prewhiten_factors is None:
        prewhitened_data = data.T
    else:
        prewhitened_data = prewhiten_factors * data.T

    # Component separation
    if nside:
        patch_ids = hp.ud_grade(np.arange(hp.nside2npix(nside)),
                                hp.npix2nside(data.shape[-1]))
        res = alg.multi_comp_sep(
            A_ev, prewhitened_data, None, A_dB_ev, comp_of_param, patch_ids,
            x0, **minimize_kwargs)
    else:
        res = alg.comp_sep(A_ev, prewhitened_data, None, A_dB_ev, comp_of_param,
                           x0, **minimize_kwargs)

    # Craft output
    # 1) Apply the mask, if any
    # 2) Restore the ordering of the input data (pixel dimension last)
    res.params = params
    res.s = res.s.T
    res.s[..., mask] = hp.UNSEEN
    res.chi = res.chi.T
    res.chi[..., mask] = hp.UNSEEN
    if 'chi_dB' in res:
        for i in range(len(res.chi_dB)):
            res.chi_dB[i] = res.chi_dB[i].T
            res.chi_dB[i][..., mask] = hp.UNSEEN
    if nside and len(x0):
        x_mask = hp.ud_grade(mask.astype(float), nside) == 1.
        res.x[x_mask] = hp.UNSEEN
        res.Sigma[x_mask] = hp.UNSEEN
        res.x = res.x.T
        res.Sigma = res.Sigma.T

    res.mask_good = ~mask
    return res


def multi_res_comp_sep(components, instrument, data, nsides, patch_ids=None, **minimize_kwargs):
    """ Basic component separation

    Parameters
    ----------
    components: list
        List storing the :class:`Component` s of the mixing matrix
    instrument
        Instrument object used to define the mixing matrix.
        It can be any object that has what follows either as a key or as an
        attribute (e.g. `dict`, `PySM.Instrument`)

        - **Frequencies**
        - **Sens_I** or **Sens_P** (optional, frequencies are inverse-noise
          weighted according to these noise levels)

    data: ndarray or MaskedArray
        Data vector to be separated. Shape *(n_freq, ..., n_pix).*
        *...* can be

        - absent or 1: temperature maps
        - 2: polarization maps
        - 3: temperature and polarization maps (see note)

        Values equal to `hp.UNSEEN` or, if `MaskedArray`, masked values are
        neglected during the component separation process.
    nsides: seq
        Specify the ``nside`` for each free parameter of the components

    patch_ids: list of pixels
        Each element of the list corresponds to a spectral index.
        For a given spectral index, the element of patch_ids collects
        the pixels ids belonging to a given cluster
        e.g. patch_ids = [clusters_b0, clusters_b1, ...]
        with clusters_b0 = [[sky pixels in cluster#1], [sky pixels in cluster#2], ...]
        
    Returns
    -------
    result: dict
	It includes

	- **param**: *(list)* - Names of the parameters fitted
	- **x**: *(seq)* - ``x[i]`` is the best-fit (map of) the *i*-th
          parameter. The map has ``nside = nsides[i]``
        - **Sigma**: *(ndarray)* - ``Sigma[i, j]`` is the (map of) the
          semi-analytic covariance between the *i*-th and the *j*-th parameter.
          It is meaningful only in the high signal-to-noise regime and when the
          *cov* is the true covariance of the data
        - **s**: *(ndarray)* - Component amplitude maps
        - **mask_good**: *(ndarray)* - mask of the entries actually used in the
          component separation

    Note
    ----

    * During the component separation, a pixel is masked if at least one of
      its frequencies is masked.
    * If you provide temperature and polarization maps, they will constrain the
      **same** set of parameters. In particular, separation is **not** done
      independently for temperature and polarization. If you want an
      independent fitting for temperature and polarization, please launch

      >>> res_T = basic_comp_sep(component_T, instrument, data[:, 0], **kwargs)
      >>> res_P = basic_comp_sep(component_P, instrument, data[:, 1:], **kwargs)

    """
    instrument = _force_keys_as_attributes(instrument)
    max_nside = max(nsides)
    if max_nside == 0:
        return basic_comp_sep(components, instrument, data, **minimize_kwargs)

    # Prepare mask and set to zero all the frequencies in the masked pixels:
    # NOTE: mask are bad pixels
    mask = _intersect_mask(data)
    data = hp.pixelfunc.ma_to_array(data).copy()
    data[..., mask] = 0  # Thus no contribution to the spectral likelihood

    try:
        data_nside = hp.get_nside(data[0])
    except TypeError:
        raise ValueError("data has to be a stack of healpix maps")

    invN = _get_prewhiten_factors(instrument, data.shape, data_nside)
    invN = np.diag(invN**2)

    if patch_ids is None:
        def array2maps(x):
            i = 0
            maps = []
            for nside in nsides:
                npix = _my_nside2npix(nside)
                maps.append(x[i:i+npix])
                i += npix
            return maps

        extra_dim = [1]*(data.ndim-1)
        unpack = lambda x: [
            _my_ud_grade(m, max_nside).reshape(-1, *extra_dim)
            for m in array2maps(x)]

        # Transpose the data and put the pixels that share the same spectral
        # indices next to each other
        n_pix_max_nside = hp.nside2npix(max_nside)
        pix_ids = np.argsort(hp.ud_grade(np.arange(n_pix_max_nside), data_nside))
        data = data.T[pix_ids].reshape(
            n_pix_max_nside, (data_nside // max_nside)**2, *data.T.shape[1:])
        back_pix_ids = np.argsort(pix_ids)

        x0 = [x for c in components for x in c.defaults]
        x0 = [np.full(_my_nside2npix(nside), px0) for nside, px0 in zip(nsides, x0)]
        x0 = np.concatenate(x0)

    else:
        ## new unpack
        # unpack x |-> list of full resolution maps
        # patch_ids will be new argument of the above function
        # giving the list of [patch_ids_Bd,patch_ids_Td, ...]
        # with patch_ids_Bd = a list of lists of pixels which 
        # will define each cluster.
        N_params = len(patch_ids)
        N_clusters = [len(patch_ids[b]) for b in range(len(patch_ids))]
        extra_dim = [1]*(data.ndim-1)
        def array2maps(x):
            clustered_map = []
            i = 0
            # loop over spectral indices
            for b in range(N_params):
                c_map = np.zeros(hp.nside2npix(data_nside), dtype=type(x[0]))
                # loop over clusters for a five spectral index
                for c in range(N_clusters[b]):
                    # and looping over pixels now
                    for p in patch_ids[b][c]:
                        c_map[p] = x[i]
                    i+=1
                clustered_map.append(c_map)
            return clustered_map
        unpack = lambda x: [c.reshape(-1,*extra_dim) for c in array2maps(x)]

        x0_ = [x for c in components for x in c.defaults]
        x0 = []
        for ix in range(len(x0_)):
            for nc in range(len(patch_ids[ix])):
                x0.append(x0_[ix])
        
        data = data.T.reshape(
            data.T.shape[0], 1, *data.T.shape[1:])
        pix_ids = np.argsort(np.arange(hp.nside2npix(data_nside)))
        back_pix_ids = np.argsort(pix_ids)

    A = MixingMatrix(*components)
    assert A.n_param == len(nsides), (
        "%i free parameters but %i nsides" % (len(A.defaults), len(nsides)))
    A_ev = A.evaluator(instrument.Frequencies, unpack)
    A_dB_ev = A.diff_evaluator(instrument.Frequencies, unpack)

    if not len(x0):
        A_ev = A_ev()

    if patch_ids is None:
        comp_of_dB = [
            (c_db, _my_ud_grade(np.arange(_my_nside2npix(p_nside)), max_nside))
            for p_nside, c_db in zip(nsides, A.comp_of_dB)]
    else:
        vec = []
        for b in range(N_params):
            vec += list(np.arange(N_clusters[b]))
        comp_of_dB = [
            (c_db, _my_ud_grade(v, data_nside))
            for v, c_db in zip(array2maps(vec), A.comp_of_dB)]

    # Component separation
    res = alg.comp_sep(A_ev, data, invN, A_dB_ev, comp_of_dB, x0,
                       **minimize_kwargs)
    # Craft output
    # 1) Apply the mask, if any
    # 2) Restore the ordering of the input data (pixel dimension last)
    def restore_index_mask_transpose(x):
        x = x.reshape(-1, *x.shape[2:])[back_pix_ids]
        x[mask] = hp.UNSEEN
        return x.T

    res.params = A.params
    res.s = restore_index_mask_transpose(res.s)
    res.chi = restore_index_mask_transpose(res.chi)
    if 'chi_dB' in res:
        for i in range(len(res.chi_dB)):
            res.chi_dB[i] = restore_index_mask_transpose(res.chi_dB[i])

    if len(x0):
        if patch_ids is None:
            x_masks = [_my_ud_grade(mask.astype(float), nside) == 1.
                   for nside in nsides]
        else: 
            x_masks = [_my_ud_grade(mask.astype(float), data_nside) == 1.
                   for b in range(N_params)]            
        res.x = array2maps(res.x)
        for x, x_mask in zip(res.x, x_masks):
                x[x_mask] = hp.UNSEEN

    res.mask_good = ~mask
    return res


def harmonic_ilc(components, instrument, data, lbins=None, weights=None, iter=3):
    """ Internal Linear Combination

    Parameters
    ----------
    components: list or tuple of lists
        `Components` of the mixing matrix. They must have no free parameter.
    instrument: dict or PySM.Instrument
        Instrument object used to define the mixing matrix
        It is required to have:

        - Frequencies

        It may have

        - Beams (FWHM in arcmin) they are deconvolved before ILC

    data: ndarray or MaskedArray
        Data vector to be separated. Shape `(n_freq, ..., n_pix)`. `...` can be
        1, 3 or absent.
        Values equal to hp.UNSEEN or, if MaskedArray, masked values are
        neglected during the component separation process.
    lbins: array
        It stores the edges of the bins that will have the same ILC weights.
    weights: array
        If provided data are multiplied by the weights map before computing alms

    Returns
    -------
    result : dict
	It includes

        - **W**: *(ndarray)* - ILC weights for each component and possibly each
          patch.
        - **freq_cov**: *(ndarray)* - Empirical covariance for each bin
        - **s**: *(ndarray)* - Component maps
        - **cl_in**: *(ndarray)* - anafast output of the input
        - **cl_out**: *(ndarray)* - anafast output of the output

    Note
    ----

    * During the component separation, a pixel is masked if at least one of its
      frequencies is masked.
    * Output spectra are divided by the fsky. fsky is computed with the MASTER
      formula if `weights` is provided, otherwise it is the fraction of unmasked
      pixels

    """
    instrument = _force_keys_as_attributes(instrument)
    nside = hp.get_nside(data[0])
    lmax = 3 * nside - 1
    lmax = min(lmax, lbins.max())
    n_comp = len(components)
    if weights is not None:
        assert not np.any(_intersect_mask(data) * weights.astype(bool)), \
            "Weights are non-zero where the data is masked"
        fsky = np.mean(weights**2)**2 / np.mean(weights**4)
    else:
        mask = _intersect_mask(data)
        fsky = float(mask.sum()) / mask.size

    logging.info('Computing alms')
    try:
        assert np.any(instrument.Beams)
    except (AttributeError, AssertionError):
        beams = None
    else:  # Deconvolve the beam
        beams = instrument.Beams

    alms = _get_alms(data, beams, lmax, weights, iter=iter)

    logging.info('Computing ILC')
    res = _harmonic_ilc_alm(components, instrument, alms, lbins, fsky)

    logging.info('Back to real')
    res.s = np.empty((n_comp,) + data.shape[1:], dtype=data.dtype)
    for c in range(n_comp):
        res.s[c] = hp.alm2map(alms[c], nside)

    return res


def _get_alms(data, beams=None, lmax=None, weights=None, iter=3):
    alms = []
    for f, fdata in enumerate(data):
        if weights is None:
            alms.append(hp.map2alm(fdata, lmax=lmax, iter=iter))
        else:
            alms.append(hp.map2alm(hp.ma(fdata)*weights, lmax=lmax, iter=iter))
        logging.info('%i of %i complete' % (f+1, len(data)))
    alms = np.array(alms)

    if beams is not None:
        logging.info('Correcting alms for the beams')
        for fwhm, alm in zip(beams, alms):
            bl = hp.gauss_beam(np.radians(fwhm/60.0), lmax, pol=(alm.ndim==2))
            if alm.ndim == 1:
                alm = [alm]
                bl = [bl]

            for i_alm, i_bl in zip(alm, bl.T):
                hp.almxfl(i_alm, 1.0/i_bl, inplace=True)

    return alms


def _harmonic_ilc_alm(components, instrument, alms, lbins=None, fsky=None):
    cl_in = np.array([hp.alm2cl(alm) for alm in alms])

    # Multipoles for the ILC bins
    lmax = hp.Alm.getlmax(alms.shape[-1])
    ell = hp.Alm.getlm(lmax)[0]
    if lbins is not None:
        ell = np.digitize(ell, lbins)
    # NOTE: use lmax for indexing alms, ell.max() is the maximum bin index

    # Make alms real
    alms = np.asarray(alms, order='C')
    alms = alms.view(np.float64)
    alms[..., np.arange(1, 2*(lmax+1), 2)] = hp.UNSEEN  # Mask imaginary m = 0
    ell = np.stack((ell, ell), axis=-1).reshape(-1)
    if alms.ndim > 2:  # TEB -> ILC indipendently on each Stokes
        n_stokes = alms.shape[1]
        assert n_stokes in [1, 3], "Alms must be either T only or T E B"
        alms[:, 1:, [0, 2, 2*lmax+2, 2*lmax+3]] = hp.UNSEEN  # EB for ell < 2
        ell = np.stack([ell] * n_stokes)  # Replicate ell for every Stokes
        ell += np.arange(n_stokes).reshape(-1, 1) * (ell.max() + 1) # Add offset

    res = ilc(components, instrument, alms, ell)

    # Craft output
    res.s[res.s == hp.UNSEEN] = 0.
    res.s = np.asarray(res.s, order='C').view(np.complex128)
    cl_out = np.array([hp.alm2cl(alm) for alm in res.s])

    res.cl_in = cl_in
    res.cl_out = cl_out
    if fsky:
        res.cl_in /= fsky
        res.cl_out /= fsky

    res.fsky = fsky
    lrange = np.arange(lmax+1)
    ldigitized = np.digitize(lrange, lbins)
    with np.errstate(divide='ignore', invalid='ignore'):
        res.l_ref = (np.bincount(ldigitized, lrange * 2*lrange+1)
                     / np.bincount(ldigitized, 2*lrange+1))
    res.freq_cov *= 2  # sqrt(2) missing between complex-real alm conversion
    if res.s.ndim > 2:
        res.freq_cov = res.freq_cov.reshape(n_stokes, -1, *res.freq_cov.shape[1:])
        res.W = res.W.reshape(n_stokes, -1, *res.W.shape[1:])

    return res


def ilc(components, instrument, data, patch_ids=None):
    """ Internal Linear Combination

    Parameters
    ----------
    components: list or tuple of lists
        `Components` of the mixing matrix. They must have no free parameter.
    instrument: PySM.Instrument
        Instrument object used to define the mixing matrix
        It is required to have:

        - Frequencies

        It's only role is to evaluate the `components` at the
        `instrument.Frequencies`.
    data: ndarray or MaskedArray
        Data vector to be separated. Shape `(n_freq, ..., n_pix)`. `...` can be
        also absent.
        Values equal to hp.UNSEEN or, if MaskedArray, masked values are
        neglected during the component separation process.
    patch_ids: array
        It stores the id of the region over which the ILC weights are computed
        independently. It must be broadcast-compatible with data.

    Returns
    -------
    result : dict
	It includes

        - **W**: *(ndarray)* - ILC weights for each component and possibly each
          patch.
        - **freq_cov**: *(ndarray)* - Empirical covariance for each patch
        - **s**: *(ndarray)* - Component maps

    Note
    ----
    * During the component separation, a pixel is masked if at least one of its
      frequencies is masked.
    """
    # Checks
    instrument = _force_keys_as_attributes(instrument)
    np.broadcast(data, patch_ids)
    n_freq = data.shape[0]
    assert len(instrument.Frequencies) == n_freq,\
        "The number of frequencies does not match the number of maps provided"
    n_comp = len(components)

    # Prepare mask and set to zero all the frequencies in the masked pixels:
    # NOTE: mask are good pixels
    mask = ~_intersect_mask(data)

    mm = MixingMatrix(*components)
    A = mm.eval(instrument.Frequencies)

    data = data.T
    res = OptimizeResult()
    res.s = np.full(data.shape[:-1] + (n_comp,), hp.UNSEEN)

    def ilc_patch(ids_i, i_patch):
        if not np.any(ids_i):
            return
        data_patch = data[ids_i]  # data_patch is a copy (advanced indexing)
        cov = np.cov(data_patch.reshape(-1, n_freq).T)
        # Perform the inversion of the correlation instead of the covariance.
        # This allows to meaninfully invert covariances that have very noisy
        # channels.
        assert cov.ndim == 2
        cov_regularizer = np.diag(cov)**0.5 * np.diag(cov)[:, np.newaxis]**0.5
        correlation = cov / cov_regularizer
        try:
            inv_freq_cov = np.linalg.inv(correlation) / cov_regularizer
        except np.linalg.LinAlgError:
            np.set_printoptions(precision=2)
            logging.error(
                "Empirical covariance matrix cannot be reliably inverted.\n"
                "The domain that failed is {i_patch}.\n"
                "Covariance matrix diagonal {np.diag(cov)}\n"
                "Correlation matrix\n{correlation}")
            raise
        res.freq_cov[i_patch] = cov
        res.W[i_patch] = alg.W(A, inv_freq_cov)
        res.s[ids_i] = alg._mv(res.W[i_patch], data_patch)

    if patch_ids is None:
        res.freq_cov = np.full((n_freq, n_freq), hp.UNSEEN)
        res.W = np.full((n_comp, n_freq), hp.UNSEEN)
        ilc_patch(mask, np.s_[:])
    else:
        n_id = patch_ids.max() + 1
        res.freq_cov = np.full((n_id, n_freq, n_freq), hp.UNSEEN)
        res.W = np.full((n_id, n_comp, n_freq), hp.UNSEEN)
        patch_ids_bak = patch_ids.copy().T
        patch_ids_bak[~mask] = -1
        for i in range(n_id):
            ids_i = np.where(patch_ids_bak == i)
            ilc_patch(ids_i, i)

    res.s = res.s.T
    res.components = mm.components

    return res


def _get_prewhiten_factors(instrument, data_shape, nside):
    """ Derive the prewhitening factor from the sensitivity

    Parameters
    ----------
    instrument: PySM.Instrument
    data_shape: tuple
        It is expected to be `(n_freq, n_stokes, n_pix)`. `n_stokes` is used to
        define if sens_I or sens_P (or both) should be used to compute the
        factors.

        - If `n_stokes` is absent or `n_stokes == 1`, use sens_I.
        - If `n_stokes == 2`, use sens_P.
        - If `n_stokes == 3`, the factors will have shape (3, n_freq). Sens_I is
          used for [0, :], while sens_P is used for [1:, :].

    Returns
    -------
    factor: array
        prewhitening factors
    """
    try:
        if len(data_shape) < 3 or data_shape[1] == 1:
            sens = instrument.Sens_I
        elif data_shape[1] == 2:
            sens = instrument.Sens_P
        elif data_shape[1] == 3:
            sens = np.stack(
                (instrument.Sens_I, instrument.Sens_P, instrument.Sens_P))
        else:
            raise ValueError(data_shape)
    except AttributeError:  # instrument has no sensitivity -> do not prewhite
        return None

    assert np.all(np.isfinite(sens))
    if nside:
        return hp.nside2resol(nside, arcmin=True) / sens
    else:
        return 12**0.5 * hp.nside2resol(1, arcmin=True) / sens


def _A_evaluator(components, instrument, prewhiten_factors=None):
    A = MixingMatrix(*components)
    A_ev = A.evaluator(instrument.Frequencies)
    A_dB_ev = A.diff_evaluator(instrument.Frequencies)
    comp_of_dB = A.comp_of_dB
    x0 = np.array([x for c in components for x in c.defaults])
    params = A.params

    if prewhiten_factors is None:
        return A_ev, A_dB_ev, comp_of_dB, x0, params

    if A.n_param:
        pw_A_ev = lambda x: prewhiten_factors[..., np.newaxis] * A_ev(x)
        pw_A_dB_ev = lambda x: [prewhiten_factors[..., np.newaxis] * A_dB_i
                                for A_dB_i in A_dB_ev(x)]
    else:
        pw_A_ev = lambda: prewhiten_factors[..., np.newaxis] * A_ev()
        pw_A_dB_ev = None

    return pw_A_ev, pw_A_dB_ev, comp_of_dB, x0, params


def _my_nside2npix(nside):
    if nside:
        return hp.nside2npix(nside)
    else:
        return 1


def _my_ud_grade(map_in, nside_out, **kwargs):
    # As healpy.ud_grade, but it accepts map_in of nside = 0 and nside_out = 0,
    # which in this module means a single float or lenght-1 array
    if nside_out == 0:
        try:
            # Both input and output hace nside = 0
            return np.array([float(map_in)])
        except TypeError:
            # This is really clunky...
            # 1) Downgrade to nside 1
            # 2) put the 12 values in the pixels of a nside 4 map that belong to
            #    the same nside 1 pixels
            # 3) Downgrade to nside 1
            # 4) pick the value of the pixel in which the 12 values were placed
            map_in = hp.ud_grade(map_in, 1, **kwargs)
            out = np.full(hp.nside2npix(4), hp.UNSEEN)
            ids = hp.ud_grade(np.arange(12), 4, **kwargs)
            out[np.where(ids == 0)[0][:12]] = map_in
            kwargs['pess'] = False
            res = hp.ud_grade(out, 1, **kwargs)
            return res[:1]
    try:
        return hp.ud_grade(np.full(12, map_in.item()),
                           nside_out, **kwargs)
    except ValueError:
        return hp.ud_grade(map_in, nside_out, **kwargs)


def _intersect_mask(maps):
    if hp.pixelfunc.is_ma(maps):
        mask = maps.mask
    else:
        mask = maps == hp.UNSEEN

    # Mask entire pixel if any of the frequencies in the pixel is masked
    return np.any(mask, axis=tuple(range(maps.ndim-1)))


# What are _LowerCaseAttrDict and _force_keys_as_attributes?
# Why are you so twisted?!? Because we decided that instrument can be either a
# PySM.Instrument or a dictionary (especially the one used to construct a
# PySM.Instrument).
#
# Suppose I want the frequencies.
# * Pysm.Instrument 
#     freqs = instrument.Frequencies
# * the configuration dict of a Pysm.Instrument
#     freqs = instrument['frequencies']
# We force the former API in the dictionary case by
# * setting all string keys to lower-case
# * making dictionary entries accessible as attributes
# * Catching upper-case attribute calls and returning the lower-case version
# Shoot us any simpler idea. Maybe dropping the PySM.Instrument support...
class _LowerCaseAttrDict(dict):
    def __init__(self, *args, **kwargs):
        super(_LowerCaseAttrDict, self).__init__(*args, **kwargs)
        for key, item in self.items():
            if isinstance(key, string_types):
                str_key = str(key)
                if str_key.lower() != str_key:
                    self[str_key.lower()] = item
                    del self[key]
        self.__dict__ = self

    def __getattr__(self, key):
        try:
            return self[key.lower()]
        except KeyError:
            raise AttributeError("No attribute named '%s'" % key)


def _force_keys_as_attributes(instrument):
    if hasattr(instrument, 'Frequencies'):
        return instrument
    else:
        return _LowerCaseAttrDict(instrument)