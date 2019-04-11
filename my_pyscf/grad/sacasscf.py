from mrh.my_pyscf.grad import lagrange
from pyscf.grad.mp2 import _shell_prange
from pyscf.mcscf import mc1step, mc1step_symm, newton_casscf
from pyscf.grad import casscf as casscf_grad
from pyscf.grad import rhf as rhf_grad
from pyscf import lib, ao2mo
import numpy as np
import copy
from functools import reduce
from scipy import linalg

def Lorb_dot_dgorb_dx (Lorb, mc, mo_coeff=None, ci=None, atmlst=None, mf_grad=None, verbose=None):
    ''' Modification of pyscf.grad.casscf.kernel to compute instead the orbital
    Lagrange term nuclear gradient (sum_pq Lorb_pq d2_Ecas/d_lambda d_kpq)
    This involves making the substitution
    (D_[p]q - D_p[q])/2 -> D_pq
    (d_[p]qrs + d_pq[r]s - d_p[q]rs - d_pqr[s])/2 -> d_pqrs
    Where [] around an index implies contraction with Lorb from the left in the
    case of a bra index (positive overall sign) and from the right in the case of a
    ket index (negative overall sign).
    Wrapping into a single effective set of mo coefficients 
    we find that since L' = -L, the transformation from MOs to 
    AOs should involve moL_coeff = mo_coeff @ L.
    We can map from transforming the density matrices to transforming the matrix elements
    by flipping the sign:
    (h_p[q] - h_[p]q)/2 -> h_pq
    (v_p[q]rs + v_pqr[s] - v_[p]qrs - v_pq[r]s)/2 -> v_pqrs
    which implies that the transforming integrals from MOs to AOs should involve
    -moL_coeff. But transforming integrals from AOs to MOs should involve
    +moL_coeff, because antisymmetric matrix!
    The CASSCF gradient here is already implemented as
    dE/dlambda = h_{p}q D_pq + 2 v_{p}qrs d_pqrs
    in the atomic orbital basis, where {p} is the total derivative
    of the given matrix element wrt to lambda but only as applied to that index.

    In other words, the permutation symmetry of indices is already fully exploited,
    and I can't reduce the number of terms in my expression any further by permutation
    symmetry of the rdm element indices. So I have to contract each and every index,
    and I have to be sure that I do it exactly once for each term (in d_xyii = D_xy D_ii
    and d_xiiy = - D_xy D_ii/2 terms, don't multiply a transformed D by a transformed D). '''

    # dmo = smoT.dao.smo
    # dao = mo.dmo.moT

    if mo_coeff is None: mo_coeff = mc.mo_coeff
    if ci is None: ci = mc.ci
    if mf_grad is None: mf_grad = mc._scf.nuc_grad_method()
    if mc.frozen is not None:
        raise NotImplementedError

    mol = mc.mol
    ncore = mc.ncore
    ncas = mc.ncas
    nocc = ncore + ncas
    nelecas = mc.nelecas
    nao, nmo = mo_coeff.shape
    nao_pair = nao * (nao+1) // 2

    mo_occ = mo_coeff[:,:nocc]
    mo_core = mo_coeff[:,:ncore]
    mo_cas = mo_coeff[:,ncore:nocc]

    # MRH: new 'effective' MO coefficients including contraction from the Lagrange multipliers
    moL_coeff = mo_coeff @ Lorb
    smo_coeff = mc._scf.get_ovlp () @ mo_coeff
    smoL_coeff = smo_coeff @ Lorb
    moL_occ = moL_coeff[:,:nocc]
    moL_core = moL_coeff[:,:ncore]
    moL_cas = moL_coeff[:,ncore:nocc]
    smo_occ = smo_coeff[:,:nocc]
    smo_core = smo_coeff[:,:ncore]
    smo_cas = smo_coeff[:,ncore:nocc]
    smoL_occ = smoL_coeff[:,:nocc]
    smoL_core = smoL_coeff[:,:ncore]
    smoL_cas = smoL_coeff[:,ncore:nocc]

    # MRH: these SHOULD be state-averaged! Use the actual sacasscf object!
    casdm1, casdm2 = mc.fcisolver.make_rdm12(ci, ncas, nelecas)

    # gfock = Generalized Fock, Adv. Chem. Phys., 69, 63
    # MRH: each index exactly once!
    dm_core = np.dot(mo_core, mo_core.T) * 2
    dm_cas = reduce(np.dot, (mo_cas, casdm1, mo_cas.T))
    # MRH: new density matrix terms
    dmL_core = np.dot(moL_core, mo_core.T)
    dmL_cas = reduce(np.dot, (moL_cas, casdm1, mo_cas.T))/2
    dmL_core += dmL_core.T
    dmL_cas += dmL_cas.T
    dm1 = dm_core + dm_cas
    dm1L = dmL_core + dmL_cas
    # MRH: end new density matrix terms
    # MRH: wrap the integral instead of the density matrix. I THINK the sign is the same!
    # mo sets 0 and 2 should be transposed, 1 and 3 should be not transposed; this will lead to correct sign
    aapa  = ao2mo.kernel(mol, (moL_cas, mo_cas, mo_occ, mo_cas), compact=False)
    aapa += ao2mo.kernel(mol, (mo_cas, moL_cas, mo_occ, mo_cas), compact=False)
    aapa += ao2mo.kernel(mol, (mo_cas, mo_cas, moL_occ, mo_cas), compact=False)
    aapa += ao2mo.kernel(mol, (mo_cas, mo_cas, mo_occ, moL_cas), compact=False)
    aapa = aapa.reshape(ncas,ncas,nocc,ncas)
    # MRH: new vhf terms
    vj, vk   = mc._scf.get_jk(mol, (dm_core,  dm_cas))
    vjL, vkL = mc._scf.get_jk(mol, (dmL_core, dmL_cas))
    h1 = mc.get_hcore()
    vhf_c = vj[0] - vk[0] * .5
    vhf_a = vj[1] - vk[1] * .5
    vhfL_c = vjL[0] - vkL[0] * .5
    vhfL_a = vjL[1] - vkL[1] * .5
    # MRH: I rewrote this Feff calculation completely, double-check it
    gfock  = h1 @ dm1L # h1e 
    gfock += (vhf_c + vhf_a) @ dmL_core # core-core and active-core, 2nd 1RDM linked
    gfock += (vhfL_c + vhfL_a) @ dm_core # core-core and active-core, 1st 1RDM linked
    gfock += vhfL_c @ dm_cas # core-active, 1st 1RDM linked
    gfock += vhf_c @ dmL_cas # core-active, 2nd 1RDM linked
    gfock += mo_occ @ np.einsum('uviw,vuwt->it', aapa, casdm2) @ mo_cas.T # active-active
    dme0 = (gfock+gfock.T)*.5
    aapa = vj = vk = vhf_c = vhf_a = h1 = gfock = None

    vhf1c, vhf1a, vhf1cL, vhf1aL = mf_grad.get_veff(mol, (dm_core, dm_cas, dmL_core, dmL_cas))
    hcore_deriv = mf_grad.hcore_generator(mol)
    s1 = mf_grad.get_ovlp(mol)

    diag_idx = np.arange(nao)
    diag_idx = diag_idx * (diag_idx+1) // 2 + diag_idx
    casdm2_cc = casdm2 + casdm2.transpose(0,1,3,2)
    dm2buf = ao2mo._ao2mo.nr_e2(casdm2.reshape(ncas**2,ncas**2), mo_cas.T,
                                (0, nao, 0, nao)).reshape(ncas**2,nao,nao)
    # MRH: contract the final two indices of the active-active 2RDM with L as you change to AOs
    # note tensordot always puts indices in the order of the arguments.
    dm2Lbuf = np.zeros ((ncas**2,nmo,nmo))
    dm2Lbuf[:,:,ncore:nocc] = np.tensordot (Lorb[:,ncore:nocc], casdm2_cc, axes=(1,2)).transpose (1,2,0,3).reshape (ncas**2,nmo,ncas)
    dm2Lbuf[:,ncore:nocc,:] = -np.tensordot (casdm2_cc, Lorb[ncore:nocc,:], axes=1).reshape (ncas**2,ncas,nmo)
    dm2Lbuf = np.ascontiguousarray (dm2Lbuf)
    dm2Lbuf = ao2mo._ao2mo.nr_e2(dm2Lbuf.reshape (ncas**2,nmo**2), mo_coeff.T,
                                (0, nao, 0, nao)).reshape(ncas**2,nao,nao)
    dm2buf = lib.pack_tril(dm2buf)
    dm2buf[:,diag_idx] *= .5
    dm2buf = dm2buf.reshape(ncas,ncas,nao_pair)
    dm2Lbuf = lib.pack_tril(dm2Lbuf)
    dm2Lbuf[:,diag_idx] *= .5
    dm2Lbuf = dm2Lbuf.reshape(ncas,ncas,nao_pair)
    casdm2 = casdm2_cc = None

    if atmlst is None:
        atmlst = list (range(mol.natm))
    aoslices = mol.aoslice_by_atom()
    de = np.zeros((len(atmlst),3))

    max_memory = mc.max_memory - lib.current_memory()[0]
    blksize = int(max_memory*.9e6/8 / ((aoslices[:,3]-aoslices[:,2]).max()*nao_pair))
    blksize = min(nao, max(2, blksize))

    for k, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = aoslices[ia]
        h1ao = hcore_deriv(ia)
        # MRH: h1e and Feff terms
        de[k] += np.einsum('xij,ij->x', h1ao, dm1L)
        de[k] -= np.einsum('xij,ij->x', s1[:,p0:p1], dme0[p0:p1]) * 2

        q1 = 0
        for b0, b1, nf in _shell_prange(mol, 0, mol.nbas, blksize):
            q0, q1 = q1, q1 + nf
            dm2_ao  = lib.einsum('ijw,pi,qj->pqw', dm2Lbuf, mo_cas[p0:p1], mo_cas[q0:q1])
            # MRH: now contract the first two indices of the active-active 2RDM with L as you go from MOs to AOs
            dm2_ao += lib.einsum('ijw,pi,qj->pqw', dm2buf, moL_cas[p0:p1], mo_cas[q0:q1])
            dm2_ao += lib.einsum('ijw,pi,qj->pqw', dm2buf, mo_cas[p0:p1], moL_cas[q0:q1])
            shls_slice = (shl0,shl1,b0,b1,0,mol.nbas,0,mol.nbas)
            eri1 = mol.intor('int2e_ip1', comp=3, aosym='s2kl',
                             shls_slice=shls_slice).reshape(3,p1-p0,nf,nao_pair)
            # MRH: I still don't understand why there is a minus here!
            de[k] -= np.einsum('xijw,ijw->x', eri1, dm2_ao) * 2
            eri1 = None
        # MRH: core-core and core-active 2RDM terms
        de[k] += np.einsum('xij,ij->x', vhf1c[:,p0:p1], dm1L[p0:p1]) * 2
        de[k] += np.einsum('xij,ij->x', vhf1cL[:,p0:p1], dm1[p0:p1]) * 2
        # MRH: active-core 2RDM terms
        de[k] += np.einsum('xij,ij->x', vhf1a[:,p0:p1], dmL_core[p0:p1]) * 2
        de[k] += np.einsum('xij,ij->x', vhf1aL[:,p0:p1], dm_core[p0:p1]) * 2

    # MRH: deleted the nuclear-nuclear part to avoid double-counting

    return de

def Lci_dot_dgci_dx (Lci, weights, mc, mo_coeff=None, ci=None, atmlst=None, mf_grad=None, verbose=None):
    ''' Modification of pyscf.grad.casscf.kernel to compute instead the CI
    Lagrange term nuclear gradient (sum_IJ Lci_IJ d2_Ecas/d_lambda d_PIJ)
    This involves removing all core-core and nuclear-nuclear terms and making the substitution
    sum_I w_I<L_I|p'q|I> + c.c. -> <0|p'q|0>
    sum_I w_I<L_I|p'r'sq|I> + c.c. -> <0|p'r'sq|0>
    The active-core terms (sum_I w_I<L_I|x'iyi|I>, sum_I w_I <L_I|x'iiy|I>, c.c.) must be retained.
    For simplicity the Lci should be weight-summed already on entry. '''
    if mo_coeff is None: mo_coeff = mc.mo_coeff
    if ci is None: ci = mc.ci
    if mf_grad is None: mf_grad = mc._scf.nuc_grad_method()
    if mc.frozen is not None:
        raise NotImplementedError

    mol = mc.mol
    ncore = mc.ncore
    ncas = mc.ncas
    nocc = ncore + ncas
    nelecas = mc.nelecas
    nao, nmo = mo_coeff.shape
    nao_pair = nao * (nao+1) // 2
    nroots = ci.shape[0]

    mo_occ = mo_coeff[:,:nocc]
    mo_core = mo_coeff[:,:ncore]
    mo_cas = mo_coeff[:,ncore:nocc]

    # MRH: TDMs + c.c. instead of RDMs
    casdm1 = np.zeros ((nroots, ncas, ncas))
    casdm2 = np.zeros ((nroots, ncas, ncas, ncas, ncas))
    for iroot in range (nroots):
        print ("norm of Lci, ci for root {}: {} {}".format (iroot, linalg.norm (Lci[iroot]), linalg.norm (ci[iroot])))
        casdm1[iroot], casdm2[iroot] = mc.fcisolver.trans_rdm12 (Lci[iroot], ci[iroot], ncas, nelecas)
    casdm1 = (casdm1 * weights[:,None,None]).sum (0)
    casdm2 = (casdm2 * weights[:,None,None,None,None]).sum (0)
    casdm1 += casdm1.transpose (1,0)
    casdm2 += casdm2.transpose (1,0,3,2)

# gfock = Generalized Fock, Adv. Chem. Phys., 69, 63
    dm_core = np.dot(mo_core, mo_core.T) * 2
    dm_cas = reduce(np.dot, (mo_cas, casdm1, mo_cas.T))
    aapa = ao2mo.kernel(mol, (mo_cas, mo_cas, mo_occ, mo_cas), compact=False)
    aapa = aapa.reshape(ncas,ncas,nocc,ncas)
    vj, vk = mc._scf.get_jk(mol, (dm_core, dm_cas))
    h1 = mc.get_hcore()
    vhf_c = vj[0] - vk[0] * .5
    vhf_a = vj[1] - vk[1] * .5
    # MRH: delete h1 + vhf_c from the first line below (core and core-core stuff)
    gfock = reduce(np.dot, (mo_occ.T, vhf_a, mo_occ)) * 2
    gfock[:,ncore:nocc] = reduce(np.dot, (mo_occ.T, h1 + vhf_c, mo_cas, casdm1))
    gfock[:,ncore:nocc] += np.einsum('uviw,vuwt->it', aapa, casdm2)
    dme0 = reduce(np.dot, (mo_occ, (gfock+gfock.T)*.5, mo_occ.T))
    aapa = vj = vk = vhf_c = vhf_a = h1 = gfock = None

    vhf1c, vhf1a = mf_grad.get_veff(mol, (dm_core, dm_cas))
    hcore_deriv = mf_grad.hcore_generator(mol)
    s1 = mf_grad.get_ovlp(mol)

    diag_idx = np.arange(nao)
    diag_idx = diag_idx * (diag_idx+1) // 2 + diag_idx
    casdm2_cc = casdm2 + casdm2.transpose(0,1,3,2)
    dm2buf = ao2mo._ao2mo.nr_e2(casdm2_cc.reshape(ncas**2,ncas**2), mo_cas.T,
                                (0, nao, 0, nao)).reshape(ncas**2,nao,nao)
    dm2buf = lib.pack_tril(dm2buf)
    dm2buf[:,diag_idx] *= .5
    dm2buf = dm2buf.reshape(ncas,ncas,nao_pair)
    casdm2 = casdm2_cc = None

    if atmlst is None:
        atmlst = range(mol.natm)
    aoslices = mol.aoslice_by_atom()
    de = np.zeros((len(atmlst),3))

    max_memory = mc.max_memory - lib.current_memory()[0]
    blksize = int(max_memory*.9e6/8 / ((aoslices[:,3]-aoslices[:,2]).max()*nao_pair))
    blksize = min(nao, max(2, blksize))

    for k, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = aoslices[ia]
        h1ao = hcore_deriv(ia)
        # MRH: dm1 -> dm_cas in the line below
        de[k] += np.einsum('xij,ij->x', h1ao, dm_cas)
        de[k] -= np.einsum('xij,ij->x', s1[:,p0:p1], dme0[p0:p1]) * 2

        q1 = 0
        for b0, b1, nf in _shell_prange(mol, 0, mol.nbas, blksize):
            q0, q1 = q1, q1 + nf
            dm2_ao = lib.einsum('ijw,pi,qj->pqw', dm2buf, mo_cas[p0:p1], mo_cas[q0:q1])
            shls_slice = (shl0,shl1,b0,b1,0,mol.nbas,0,mol.nbas)
            eri1 = mol.intor('int2e_ip1', comp=3, aosym='s2kl',
                             shls_slice=shls_slice).reshape(3,p1-p0,nf,nao_pair)
            de[k] -= np.einsum('xijw,ijw->x', eri1, dm2_ao) * 2
            eri1 = None
        # MRH: dm1 -> dm_cas in the line below
        de[k] += np.einsum('xij,ij->x', vhf1c[:,p0:p1], dm_cas[p0:p1]) * 2
        de[k] += np.einsum('xij,ij->x', vhf1a[:,p0:p1], dm_core[p0:p1]) * 2

    return de


class Gradients (lagrange.Gradients):

    def __init__(self, mc):
        self.__dict__.update (mc.__dict__)
        nmo = mc.mo_coeff.shape[-1]
        self.ngorb = np.count_nonzero (mc.uniq_var_indices (nmo, mc.ncore, mc.ncas, mc.frozen))
        self.nci = mc.fcisolver.nroots * mc.ci[0].size
        self.nroots = mc.fcisolver.nroots
        self.iroot = mc.nuc_grad_iroot
        self.eris = None
        self.weights = np.array ([1])
        if hasattr (mc, 'weights'):
            self.weights = np.asarray (mc.weights)
        assert (len (self.weights) == self.nroots), '{} {}'.format (self.weights, self.nroots)
        lagrange.Gradients.__init__(self, mc.mol, self.ngorb+self.nci, mc)

    def make_fcasscf (self, casscf_attr={}, fcisolver_attr={}):
        ''' Make a fake CASSCF object for ostensible single-state calculations '''
        if isinstance (self.base, mc1step_symm.CASSCF):
            fcasscf = mc1step_symm.CASSCF (self.base._scf, self.base.ncas, self.base.nelecas)
        else:
            fcasscf = mc1step.CASSCF (self.base._scf, self.base.ncas, self.base.nelecas)
        fcasscf.__dict__.update (self.base.__dict__)
        if hasattr (self.base, 'weights'):
            fcasscf.fcisolver = self.base.fcisolver._base_class (self.base.mol)
            fcasscf.fcisolver.__dict__.update (self.base.fcisolver.__dict__)
        fcasscf.__dict__.update (casscf_attr)
        fcasscf.fcisolver.__dict__.update (fcisolver_attr)
        return fcasscf

    def kernel (self, iroot=None, atmlst=None, verbose=None, mo=None, ci=None, eris=None, mf_grad=None, **kwargs):
        if iroot is None: iroot = self.iroot
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if eris is None and self.eris is None:
            eris = self.eris = self.base.ao2mo (mo)
        elif eris is None:
            eris = self.eris
        if mf_grad is None: mf_grad = self.base._scf.nuc_grad_method ()
        return super().kernel (iroot=iroot, atmlst=atmlst, verbose=verbose, mo=mo, ci=ci, eris=eris, mf_grad=mf_grad)

    def get_wfn_response (self, atmlst=None, iroot=None, verbose=None, mo=None, ci=None, eris=None, **kwargs):
        if iroot is None: iroot = self.iroot
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if eris is None and self.eris is None:
            eris = self.eris = self.base.ao2mo (mo)
        elif eris is None:
            eris = self.eris
        ndet = ci[iroot].size
        fcasscf = self.make_fcasscf ()
        g_all_iroot = newton_casscf.gen_g_hop (fcasscf, mo, ci[iroot], eris, verbose)[0]
        g_all = np.zeros (self.nlag)
        g_all[:self.ngorb] = g_all_iroot[:self.ngorb]
        # No need to reshape or anything, just use the magic of repeated slicing
        g_all[self.ngorb:][ndet*iroot:][:ndet] = g_all_iroot[self.ngorb:]
        return g_all

    def get_Lop_Ldiag (self, atmlst=None, iroot=None, verbose=None, mo=None, ci=None, eris=None, **kwargs):
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if eris is None and self.eris is None:
            eris = self.eris = self.base.ao2mo (mo)
        elif eris is None:
            eris = self.eris
        Lop, Ldiag = newton_casscf.gen_g_hop (self.base, mo, ci, eris, verbose)[2:]
        return Lop, Ldiag

    def get_ham_response (self, iroot=None, atmlst=None, verbose=None, mo=None, ci=None, eris=None, mf_grad=None, **kwargs):
        if iroot is None: iroot = self.iroot
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci[iroot]
        if eris is None and self.eris is None:
            eris = self.eris = self.base.ao2mo (mo)
        elif eris is None:
            eris = self.eris
        return casscf_grad.kernel (self.base, mo_coeff=mo, ci=ci, atmlst=atmlst, mf_grad=mf_grad, verbose=verbose)

    def get_LdotJnuc (self, Lvec, iroot=None, atmlst=None, verbose=None, mo=None, ci=None, eris=None, mf_grad=None, **kwargs):
        if iroot is None: iroot = self.iroot
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci[iroot]
        if eris is None and self.eris is None:
            eris = self.eris = self.base.ao2mo (mo)
        elif eris is None:
            eris = self.eris
        ncas = self.base.ncas
        nelecas = self.base.nelecas
        if getattr(self.base.fcisolver, 'gen_linkstr', None):
            linkstr  = self.base.fcisolver.gen_linkstr(ncas, nelecas, False)
        else:
            linkstr  = None

        # Just sum the weights now... Lorb can be implicitly summed
        # Lci may be in the csf basis
        Lorb = self.base.unpack_uniq_var (Lvec[:self.ngorb])
        Lci = Lvec[self.ngorb:].reshape (self.nroots, -1)
        ci = np.ravel (ci).reshape (self.nroots, -1)

        # CI part
        de_Lci = Lci_dot_dgci_dx (Lci, self.weights, self.base, mo_coeff=mo, ci=ci, atmlst=atmlst, mf_grad=mf_grad, verbose=verbose)
        lib.logger.info(self, '--------------- %s gradient Lagrange CI response ---------------',
                    self.base.__class__.__name__)
        if verbose >= lib.logger.INFO: rhf_grad._write(self, self.mol, de_Lci, atmlst)
        lib.logger.info(self, '----------------------------------------------------------------')

        # Orb part
        de_Lorb = Lorb_dot_dgorb_dx (Lorb, self.base, mo_coeff=mo, ci=ci, atmlst=atmlst, mf_grad=mf_grad, verbose=verbose)
        lib.logger.info(self, '--------------- %s gradient Lagrange orbital response ---------------',
                    self.base.__class__.__name__)
        if verbose >= lib.logger.INFO: rhf_grad._write(self, self.mol, de_Lorb, atmlst)
        lib.logger.info(self, '----------------------------------------------------------------------')

        return de_Lci + de_Lorb
    
    def debug_lagrange (self, Lvec):
        ngorb = self.ngorb
        nci = self.nci
        nroots = self.nroots
        ndet = nci // nroots
        ncore = self.base.ncore
        ncas = self.base.ncas
        nocc = ncore + ncas
        Lorb = self.base.unpack_uniq_var (Lvec[:ngorb])
        Lci = Lvec[ngorb:].reshape (nroots, ndet)
        lib.logger.debug (self, "{} gradient Lagrange factor, inactive-active orbital rotations:\n{}".format (
            self.base.__class__.__name__, Lorb[:ncore,ncore:nocc]))
        lib.logger.debug (self, "{} gradient Lagrange factor, inactive-external orbital rotations:\n{}".format (
            self.base.__class__.__name__, Lorb[:ncore,nocc:]))
        lib.logger.debug (self, "{} gradient Lagrange factor, active-external orbital rotations:\n{}".format (
            self.base.__class__.__name__, Lorb[ncore:nocc,nocc:]))

