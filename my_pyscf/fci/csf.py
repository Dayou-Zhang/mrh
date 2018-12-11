import numpy as np
import scipy
from pyscf.lib import logger, davidson1
from pyscf.fci import direct_spin1, cistring
from pyscf.fci.direct_spin1 import _unpack_nelec, _get_init_guess, kernel_ms1
from mrh.my_pyscf.fci.csdstring import make_csd_mask, make_econf_det_mask
from mrh.my_pyscf.fci.csfstring import transform_civec_det2csf, transform_civec_csf2det, transform_opmat_det2csf, count_all_csfs, make_econf_csf_mask

class FCISolver (direct_spin1.FCISolver):
    r''' get_init_guess uses csfstring.py and csdstring.py to construct a spin-symmetry-adapted initial guess, and the Davidson algorithm is carried
    out in the CSF basis. However, the ci attribute is put in the determinant basis at the end of it all, and "ci0" is also assumed
    to be in the determinant basis.'''

    def __init__(self, mol=None, smult=None):
        self.smult = smult
        self.csd_mask = self.econf_det_mask = self.econf_csf_mask = None
        self.mask_cache = [0, 0, 0, 0]
        super().__init__(mol)

    def get_init_guess(self, norb, nelec, nroots, hdiag):
        ''' The existing _get_init_guess function will work in the csf basis if I pass it with na, nb = ncsf, 1. This might change in future PySCF versions though. '''
        neleca, nelecb = _unpack_nelec (nelec)
        self.check_mask_cache ()
        hdiag_csf = transform_civec_det2csf (hdiag, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask, do_normalize=False)[0]
        ncsf = count_all_csfs (norb, neleca, nelecb, self.smult)
        assert (ncsf >= nroots), "Can't find {} roots among only {} CSFs".format (nroots, ncsf)
        ci_csf = _get_init_guess (ncsf, 1, nroots, hdiag_csf)
        ci = transform_civec_csf2det (ci_csf, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask)[0]
        return ci

    def kernel(self, h1e, eri, norb, nelec, ci0=None, smult=None,
               tol=None, lindep=None, max_cycle=None, max_space=None,
               nroots=None, davidson_only=None, pspace_size=None,
               orbsym=None, wfnsym=None, ecore=0, **kwargs):
        ''' Over the top of the existing kernel, I just need to set the parameters and cache values related to spin. '''
        if self.verbose >= logger.WARN:
            self.check_sanity()
        self.norb = norb
        self.nelec = nelec
        neleca, nelecb = _unpack_nelec (nelec)
        if smult is not None:
            self.smult = smult
        self.check_mask_cache ()
        if nroots is not None:
            ncsf = count_all_csfs (norb, neleca, nelecb, self.smult)
            assert (ncsf >= nroots), "Can't find {} roots among only {} CSFs".format (nroots, ncsf)
        self.eci, self.ci = \
                kernel_ms1(self, h1e, eri, norb, nelec, ci0, None,
                           tol, lindep, max_cycle, max_space, nroots,
                           davidson_only, pspace_size, ecore=ecore, **kwargs)
        return self.eci, self.ci


    def make_precond(self, hdiag, pspaceig, pspaceci, addr):
        ''' I need to transform hdiag, pspaceci, and addr into the CSF basis
        addr is trickiest. I match the determinant address to the electron configuration,
        and from there match the electron configuration to the CSFs. '''

        norb, smult = self.norb, self.smult
        neleca, nelecb = _unpack_nelec (self.nelec)
        self.check_mask_cache ()

        hdiag = transform_civec_det2csf (hdiag, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask, do_normalize=False)[0]
        if pspaceci is not None:
            pspaceci = transform_civec_det2csf (pspaceci, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask, vec_on_cols=True)[0]
        addr = np.nonzero (np.isin (self.econf_csf_mask, np.unique (self.econf_det_mask[addr])))[0]

        return super().make_precond (hdiag, pspaceig, pspaceci, addr)

    def eig(self, op, x0=None, precond=None, **kwargs):
        r''' op and x0 need to be put in the CSF basis. '''
        norb = self.norb
        neleca, nelecb = _unpack_nelec (self.nelec)
        self.check_mask_cache ()
        if isinstance(op, np.ndarray):
            assert (isinstance (self.smult, (int, np.number)))
            self.converged = True
            op_csf = transform_opmat_det2csf (op, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask)
            e, ci = scipy.linalg.eigh (op_csf)
            ci = transform_civec_csf2det (ci, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask, vec_on_cols=True)[0]
            return e, ci
        def op_csf (x):
            x_det = transform_civec_csf2det (x, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask)[0]
            hx_det = op (x_det)
            hx = transform_civec_det2csf (hx_det, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask, do_normalize=False)[0]
            return hx
        try:
            x0_csf = transform_civec_det2csf (x0, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask)[0]
        except AttributeError as e:
            x0_csf = lambda: transform_civec_det2csf (x0 (), norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask)[0]
        self.converged, e, ci = \
                davidson1(lambda xs: [op_csf(x) for x in xs],
                              x0_csf, precond, lessio=self.lessio, **kwargs)
        ci = transform_civec_csf2det (ci, norb, neleca, nelecb, self.smult, csd_mask=self.csd_mask)[0]
        if kwargs['nroots'] == 1:
            self.converged = self.converged[0]
            e = e[0]
            ci = ci[0]

        return e, ci

    '''
    def eig(self, op, x0=None, precond=None, **kwargs):
        if isinstance(op, numpy.ndarray):
            self.converged = True
            return scipy.linalg.eigh(op)

        self.converged, e, ci = \
                lib.davidson1(lambda xs: [op(x) for x in xs],
                              x0, precond, lessio=self.lessio, **kwargs)
        if kwargs['nroots'] == 1:
            self.converged = self.converged[0]
            e = e[0]
            ci = ci[0]
        return e, ci
    '''

    def check_mask_cache (self):
        assert (isinstance (self.smult, (int, np.number)))
        neleca, nelecb = _unpack_nelec (self.nelec)
        if self.mask_cache != [self.norb, neleca, nelecb, self.smult] or self.csd_mask is None:
            self.csd_mask = make_csd_mask (self.norb, neleca, nelecb)
            self.econf_det_mask = make_econf_det_mask (self.norb, neleca, nelecb, self.csd_mask)
            self.econf_csf_mask = make_econf_csf_mask (self.norb, neleca, nelecb, self.smult)
            self.mask_cache = [self.norb, neleca, nelecb, self.smult]
            

