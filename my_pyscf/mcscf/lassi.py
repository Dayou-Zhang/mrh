import numpy as np
import time
from scipy import linalg
from mrh.my_pyscf.mcscf import lassi_op_slow as op
from mrh.my_pyscf.mcscf import lassi_op as op_expt
from pyscf import lib, symm
from pyscf.lib.numpy_helper import tag_array
from pyscf.fci.direct_spin1 import _unpack_nelec
from itertools import combinations, product

def ham_2q (las, mo_coeff, veff_c=None, h2eff_sub=None):
    # Construct second-quantization Hamiltonian
    ncore, ncas, nocc = las.ncore, las.ncas, las.ncore + las.ncas
    mo_core = mo_coeff[:,:ncore]
    mo_cas = mo_coeff[:,ncore:nocc]
    hcore = las._scf.get_hcore ()
    if veff_c is None: 
        dm_core = 2 * mo_core @ mo_core.conj ().T
        veff_c = las.get_veff (dm1s=dm_core)
    if h2eff_sub is None:
        h2eff_sub = las.ao2mo (mo_coeff)
    e0 = las._scf.energy_nuc () + 2 * (((hcore + veff_c/2) @ mo_core) * mo_core).sum ()
    h1 = mo_cas.conj ().T @ (hcore + veff_c) @ mo_cas
    h2 = h2eff_sub[ncore:nocc].reshape (ncas*ncas, ncas * (ncas+1) // 2)
    h2 = lib.numpy_helper.unpack_tril (h2).reshape (ncas, ncas, ncas, ncas)
    return e0, h1, h2

def las_symm_tuple (las):
    # This really should be much more modular
    # Symmetry tuple: neleca, nelecb, irrep
    statesym = []
    s2_states = []
    for iroot in range (las.nroots):
        neleca = 0
        nelecb = 0
        wfnsym = 0
        s = 0
        m = []
        for fcibox, nelec in zip (las.fciboxes, las.nelecas_sub):
            solver = fcibox.fcisolvers[iroot]
            na, nb = _unpack_nelec (fcibox._get_nelec (solver, nelec))
            neleca += na
            nelecb += nb
            s_frag = (solver.smult - 1) // 2
            s += s_frag * (s_frag + 1)
            m.append ((na-nb)//2)
            fragsym = getattr (solver, 'wfnsym', 0) or 0 # in case getattr returns "None"
            if isinstance (fragsym, str):
                fragsym = symm.irrep_name2id (solver.mol.groupname, fragsym)
            assert isinstance (fragsym, (int, np.integer)), '{} {}'.format (type (fragsym), fragsym)
            wfnsym ^= fragsym
        s += sum ([2*m1*m2 for m1, m2 in combinations (m, 2)])
        s2_states.append (s)
        statesym.append ((neleca, nelecb, wfnsym))
    lib.logger.info (las, 'Symmetry analysis of LAS states:')
    lib.logger.info (las, ' {:2s}  {:>16s}  {:6s}  {:6s}  {:6s}  {:6s}'.format ('ix', 'Energy', 'Neleca', 'Nelecb', '<S**2>', 'Wfnsym'))
    for ix, (e, sy, s2) in enumerate (zip (las.e_states, statesym, s2_states)):
        neleca, nelecb, wfnsym = sy
        wfnsym = symm.irrep_id2name (las.mol.groupname, wfnsym)
        lib.logger.info (las, ' {:2d}  {:16.10f}  {:6d}  {:6d}  {:6.3f}  {:>6s}'.format (ix, e, neleca, nelecb, s2, wfnsym))

    return statesym, np.asarray (s2_states)

def lassi (las, mo_coeff=None, ci=None, veff_c=None, h2eff_sub=None, orbsym=None):
    ''' Diagonalize the state-interaction matrix of LASSCF '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if orbsym is None: 
        orbsym = getattr (las.mo_coeff, 'orbsym', None)
        if orbsym is None and callable (getattr (las, 'label_symmetry_', None)):
            orbsym = las.label_symmetry_(las.mo_coeff).orbsym
        if orbsym is not None:
            orbsym = orbsym[las.ncore:las.ncore+las.ncas]

    # Construct second-quantization Hamiltonian
    e0, h1, h2 = ham_2q (las, mo_coeff, veff_c=None, h2eff_sub=None)

    # Symmetry tuple: neleca, nelecb, irrep
    statesym, s2_states = las_symm_tuple (las)

    # Loop over symmetry blocks
    e_roots = np.zeros (las.nroots, dtype=np.float64)
    s2_roots = np.zeros (las.nroots, dtype=np.float64)
    si = np.zeros ((las.nroots, las.nroots), dtype=np.float64)    
    for rootsym in set (statesym):
        idx = np.all (np.array (statesym) == rootsym, axis=1)
        lib.logger.debug (las, 'Diagonalizing LAS state symmetry block (neleca, nelecb, irrep) = {}'.format (rootsym))
        if np.count_nonzero (idx) == 1:
            lib.logger.debug (las, 'Only one state in this symmetry block')
            e_roots[idx] = las.e_states[idx] - e0
            si[np.ix_(idx,idx)] = 1.0
            s2_roots[idx] = s2_states[idx]
            continue
        wfnsym = rootsym[-1]
        ci_blk = [[c for c, ix in zip (cr, idx) if ix] for cr in ci]
        nelec_blk = [[_unpack_nelec (fcibox._get_nelec (solver, nelecas)) for solver, ix in zip (fcibox.fcisolvers, idx) if ix] for fcibox, nelecas in zip (las.fciboxes, las.nelecas_sub)]
        ham_blk, s2_blk, ovlp_blk = op.ham (las.mol, h1, h2, ci_blk, las.ncas_sub, nelec_blk, orbsym=orbsym, wfnsym=wfnsym)
        lib.logger.debug (las, 'Block Hamiltonian - ecore:')
        lib.logger.debug (las, '{}'.format (ham_blk))
        lib.logger.debug (las, 'Block S**2:')
        lib.logger.debug (las, '{}'.format (s2_blk))
        lib.logger.debug (las, 'Block overlap matrix:')
        lib.logger.debug (las, '{}'.format (ovlp_blk))
        diag_test = np.diag (ham_blk)
        diag_ref = las.e_states[idx] - e0
        lib.logger.debug (las, '{:>13s} {:>13s} {:>13s}'.format ('Diagonal', 'Reference', 'Error'))
        for ix, (test, ref) in enumerate (zip (diag_test, diag_ref)):
            lib.logger.debug (las, '{:13.6e} {:13.6e} {:13.6e}'.format (test, ref, test-ref))
        assert (np.allclose (diag_test, diag_ref, atol=1e-5)), 'SI Hamiltonian diagonal element error. Inadequate convergence?'
        e, c = linalg.eigh (ham_blk, b=ovlp_blk)
        s2_blk = c.conj ().T @ s2_blk @ c
        lib.logger.debug (las, 'Block S**2 in adiabat basis:')
        lib.logger.debug (las, '{}'.format (s2_blk))
        e_roots[idx] = e
        s2_roots[idx] = np.diag (s2_blk)
        si[np.ix_(idx,idx)] = c
    idx = np.argsort (e_roots)
    rootsym = np.array (statesym)[idx]
    e_roots = e_roots[idx] + e0
    s2_roots = s2_roots[idx]
    nelec_roots = [statesym[ix][0:2] for ix in idx]
    wfnsym_roots = [statesym[ix][2] for ix in idx]
    si = si[:,idx]
    si = tag_array (si, s2=s2_roots, nelec=nelec_roots, wfnsym=wfnsym_roots)
    lib.logger.info (las, 'LASSI eigenvalues:')
    lib.logger.info (las, ' {:2s}  {:>16s}  {:6s}  {:6s}  {:6s}  {:6s}'.format ('ix', 'Energy', 'Neleca', 'Nelecb', '<S**2>', 'Wfnsym'))
    for ix, (er, s2r, rsym) in enumerate (zip (e_roots, s2_roots, rootsym)):
        neleca, nelecb, wfnsym = rsym
        wfnsym = symm.irrep_id2name (las.mol.groupname, wfnsym)
        lib.logger.info (las, ' {:2d}  {:16.10f}  {:6d}  {:6d}  {:6.3f}  {:>6s}'.format (ix, er, neleca, nelecb, s2r, wfnsym))
    return e_roots, si

def make_stdm12s (las, ci=None, orbsym=None):
    if ci is None: ci = las.ci
    if orbsym is None: 
        orbsym = getattr (las.mo_coeff, 'orbsym', None)
        if orbsym is None and callable (getattr (las, 'label_symmetry_', None)):
            orbsym = las.label_symmetry_(las.mo_coeff).orbsym
        if orbsym is not None:
            orbsym = orbsym[las.ncore:las.ncore+las.ncas]

    norb = las.ncas
    statesym = las_symm_tuple (las)[0]
    stdm1s = np.zeros ((las.nroots, las.nroots, 2, norb, norb),
        dtype=ci[0][0].dtype).transpose (0,2,3,4,1)
    stdm2s = np.zeros ((las.nroots, las.nroots, 2, norb, norb, 2, norb, norb),
        dtype=ci[0][0].dtype).transpose (0,2,3,4,5,6,7,1)

    for rootsym in set (statesym):
        idx = np.all (np.array (statesym) == rootsym, axis=1)
        wfnsym = rootsym[-1]
        ci_blk = [[c for c, ix in zip (cr, idx) if ix] for cr in ci]
        t0 = (time.clock (), time.time ())
        d1s, d2s = op.make_stdm12s (las, ci_blk, idx, orbsym=orbsym, wfnsym=wfnsym)
        t0 = lib.logger.timer (las, 'LASSI make_stdm12s CI algorithm', *t0)
        d1s_test, d2s_test = op_expt.make_stdm12s (las, ci_blk, idx)
        t0 = lib.logger.timer (las, 'LASSI make_stdm12s TDM algorithm', *t0)
        lib.logger.debug (las, 'LASSI make_stdm12s: D1 smart algorithm error = {}'.format (linalg.norm (d1s_test - d1s))) 
        lib.logger.debug (las, 'LASSI make_stdm12s: D2 smart algorithm error = {}'.format (linalg.norm (d2s_test - d2s))) 
        idx_int = np.where (idx)[0]
        for (i,a), (j,b) in product (enumerate (idx_int), repeat=2):
            stdm1s[a,...,b] = d1s[i,...,j]
            stdm2s[a,...,b] = d2s[i,...,j]
    return stdm1s, stdm2s

def roots_make_rdm12s (las, ci, si, orbsym=None):
    if orbsym is None: 
        orbsym = getattr (las.mo_coeff, 'orbsym', None)
        if orbsym is None and callable (getattr (las, 'label_symmetry_', None)):
            orbsym = las.label_symmetry_(las.mo_coeff).orbsym
        if orbsym is not None:
            orbsym = orbsym[las.ncore:las.ncore+las.ncas]

    # Symmetry tuple: neleca, nelecb, irrep
    norb = las.ncas
    statesym = las_symm_tuple (las)[0]
    rdm1s = np.zeros ((las.nroots, 2, norb, norb),
        dtype=ci[0][0].dtype)
    rdm2s = np.zeros ((las.nroots, 2, norb, norb, 2, norb, norb),
        dtype=ci[0][0].dtype)
    rootsym = [(ne[0], ne[1], wfnsym) for ne, wfnsym in zip (si.nelec, si.wfnsym)]

    for sym in set (statesym):
        idx_ci = np.all (np.array (statesym) == sym, axis=1)
        idx_si = np.all (np.array (rootsym)  == sym, axis=1)
        wfnsym = sym[-1]
        ci_blk = [[c for c, ix in zip (cr, idx_ci) if ix] for cr in ci]
        nelec_blk = [[_unpack_nelec (fcibox._get_nelec (solver, nelecas)) for solver, ix in zip (fcibox.fcisolvers, idx_ci) if ix] for fcibox, nelecas in zip (las.fciboxes, las.nelecas_sub)]
        si_blk = si[np.ix_(idx_ci,idx_si)]
        d1s, d2s = op.roots_make_rdm12s (las.mol, ci_blk, si_blk, las.ncas_sub, nelec_blk, orbsym=orbsym, wfnsym=wfnsym)
        idx_int = np.where (idx_si)[0]
        for (i,a) in enumerate (idx_int):
            rdm1s[a] = d1s[i]
            rdm2s[a] = d2s[i]
    return rdm1s, rdm2s






