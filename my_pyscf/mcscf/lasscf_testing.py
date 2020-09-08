import numpy as np
from scipy import linalg
from mrh.my_pyscf.mcscf import lasci
from pyscf.mcscf import mc_ao2mo
from pyscf.lo import orth

# An implementation that carries out vLASSCF, but without utilizing Schmidt decompositions
# or "fragment" subspaces, so that the orbital-optimization part scales no better than
# CASSCF. Eventually to be modified into a true all-PySCF implementation of vLASSCF

class LASSCF_UnitaryGroupGenerators (lasci.LASCI_UnitaryGroupGenerators):

    def _init_orb (self, las, mo_coeff, ci):
        lasci.LASCI_UnitaryGroupGenerators._init_orb (self, las, mo_coeff, ci)
        ncore, ncas = las.ncore, las.ncas
        nmo = mo_coeff.shape[-1]
        nocc = ncore + ncas
        self.uniq_orb_idx[ncore:nocc,:ncore] = True
        self.uniq_orb_idx[nocc:,ncore:nocc] = True
        if self.frozen is not None:
            idx[self.frozen,:] = idx[:,self.frozen] = False

class LASSCFSymm_UnitaryGroupGenerators (LASSCF_UnitaryGroupGenerators):
    _init_orb = lasci.LASCISymm_UnitaryGroupGenerators._init_orb
    # TODO: test that the "super()" command in the above points to
    # the correct parent class

class LASSCF_HessianOperator (lasci.LASCI_HessianOperator):
    # Required modifications for Hx: [I forgot about 3) at first]
    #   1) cache CASSCF-type eris and paaa - init_df
    #   2) increase range of ocm2 - make_odm1s2c_sub
    #   3) extend veff to active-unactive sector - split_veff
    #   4) dot the above three together - orbital_response
    #   5) TODO: get_veff using DF needs to be extended as well
    # Required modifications for API:
    #   6) broader ERI rotation - update_mo_ci_eri
    # Possible modifications:
    #   7) current prec may not be "good enough" - get_prec
    #   8) define "gx" in this context - get_gx 

    def _init_df (self):
        lasci.LASCI_HessianOperator._init_df (self)
        self.cas_type_eris = mc_ao2mo._ERIS (self.las, self.mo_coeff,
            method='incore', level=2) # level=2 -> ppaa, papa only
            # level=1 computes more stuff; it's only useful if I
            # want the honest hdiag in get_prec ()
        ncore, ncas = self.ncore, self.ncas
        nocc = ncore + ncas

    def make_odm1s2c_sub (self, kappa):
        # This is tricky, because in the parent I transposed ocm2 to make it 
        # symmetric upon index permutation, but if I do the same thing here then
        # I have an array of shape [nmo,]*4 (too big).
        # My options are 
        #   -1) Ignore it and just hope the optimization converges without this
        #       term (lol it won't)
        #   0) Just make an extremely limited implementation for small molecules
        #       only like the laziest motherfucker in existence
        #   1) Tag with kappa and mar the whole design of the class
        #   2) Extend one index and distinguish btwn cas-cas and other blocks
        #   3) Rewrite the whole thing to not transpose in the first place
        # Obviously 3) is the cleanest. But 2) lets me be more "pythonic" and
        #   I'll go with that for now 
        ncore, nocc = self.ncore, self.nocc
        odm1fs, ocm2_cas = lasci.LASCI_HessianOperator.make_odm1s2c_sub (self, kappa)
        kappa_ext = kappa[ncore:nocc,:].copy ()
        kappa_ext[:,ncore:nocc] = 0.0
        ocm2 = -np.dot (self.cascm2, kappa_ext) 
        ocm2[:,:,:,ncore:nocc] += ocm2_cas
        ocm2 = np.asfortranarray (ocm2) # Make largest index slowest-moving
        return odm1fs, ocm2

    def get_veff_Heff (self, odm1fs, tdm1frs):
        ncore, ncas, nmo = self.ncore, self.ncas, self.nmo
        nocc = ncore + ncas
        veff_mo, h1frs = lasci.LASCI_HessianOperator.get_veff_Heff (self, odm1fs, tdm1frs)
        # Additional error to subtract from h1frs
        for isub, odm1s in enumerate (odm1fs[1:]):
            err_dm1rs = odm1s[None,:,:,ncore:nocc].copy ()
            err_dm1rs[:,:,ncore:nocc,:] = 0.0
            err_h1rs = np.tensordot (err_dm1rs, self.h2eff_sub, axes=2)
            err_h1rs += err_h1rs[:,::-1] # ja + jb
            err_h1rs -= np.tensordot (err_dm1rs, self.h2eff_sub, axes=((2,3),(0,3)))
            err_h1rs += err_h1rs.transpose (0,1,3,2)
            h1frs[isub,:,:,:,:] -= err_h1rs
        return veff_mo, h1frs


    def split_veff (self, veff_mo, dm1s_mo):
        veff_c = veff_mo.copy ()
        ncore = self.ncore
        nocc = self.nocc
        sdm = dm1s_mo[0] - dm1s_mo[1]
        sdm_ra = sdm[:,ncore:nocc]
        sdm_ar = sdm[ncore:nocc,:].copy ()
        sdm_ar[:,ncore:nocc] = 0.0
        veff_s = np.zeros_like (veff_c)
        vk_pa = veff_s[:,ncore:nocc]
        for p, v1 in enumerate (vk_pa):
            praa = self.cas_type_eris.ppaa[p]
            para = self.cas_type_eris.papa[p]
            paaa = praa[ncore:nocc]
            v1[:]  = np.tensordot (sdm_ra, praa, axes=2)
            v1[:] += np.tensordot (sdm_ar, para, axes=2)
        veff_s[:,:] *= -0.5
        vk_aa = vk_pa[ncore:nocc]
        veff_s[ncore:nocc,:] = vk_pa.T
        assert (np.allclose (veff_s, veff_s.T)), vk_aa-vk_aa.T
        veffa = veff_c + veff_s
        veffb = veff_c - veff_s
        return np.stack ([veffa, veffb], axis=0)

    def orbital_response (self, kappa, odm1fs, ocm2, tdm1frs, tcm2, veff_prime):
        ncore, nocc, nmo = self.ncore, self.nocc, self.nmo
        ocm2_cas = ocm2[:,:,:,ncore:nocc] 
        gorb = lasci.LASCI_HessianOperator.orbital_response (self, kappa, odm1fs,
            ocm2_cas, tdm1frs, tcm2, veff_prime)
        f1_prime = np.zeros ((self.nmo, self.nmo), dtype=self.dtype)
        ecm2 = ocm2_cas + tcm2
        f1_prime[:ncore,ncore:nocc] = np.tensordot (self.h2eff_sub[:ncore], ecm2, axes=((1,2,3),(1,2,3)))
        f1_prime[nocc:,ncore:nocc] = np.tensordot (self.h2eff_sub[nocc:], ecm2, axes=((1,2,3),(1,2,3)))
        for p, f1 in enumerate (f1_prime):
            praa = self.cas_type_eris.ppaa[p]
            para = self.cas_type_eris.papa[p]
            paaa = praa[ncore:nocc]
            # g_pabc d_qabc + g_prab d_qrab + g_parb d_qarb + g_pabr d_qabr (Formal)
            #        d_cbaq          d_abqr          d_aqbr          d_qabr (Symmetry of ocm2)
            # g_pcba d_abcq + g_prab d_abqr + g_parc d_aqcr + g_pbcr d_qbcr (Relabel)
            #                                                 g_pbrc        (Symmetry of eri)
            # g_pcba d_abcq + g_prab d_abqr + g_parc d_aqcr + g_pbrc d_qbcr (Final)
            for i, j in ((0, ncore), (nocc, nmo)): # Don't double-count
                ra, ar, cm = praa[i:j], para[:,i:j], ocm2[:,:,:,i:j]
                f1[i:j] += np.tensordot (paaa, cm, axes=((0,1,2),(2,1,0))) # last index external
                f1[ncore:nocc] += np.tensordot (ra, cm, axes=((0,1,2),(3,0,1))) # third index external
                f1[ncore:nocc] += np.tensordot (ar, cm, axes=((0,1,2),(0,3,2))) # second index external
                f1[ncore:nocc] += np.tensordot (ar, cm, axes=((0,1,2),(2,3,1))) # first index external
        return gorb + (f1_prime - f1_prime.T)

    def update_mo_ci_eri (self, x, h2eff_sub):
        # mo and ci are fine, but h2eff sub simply has to be replaced
        mo1, ci1 = lasci.LASCI_HessianOperator.update_mo_ci_eri (self, x, h2eff_sub)[:2]
        return mo1, ci1, self.las.ao2mo (mo1)


class LASSCFNoSymm (lasci.LASCINoSymm):
    get_ugg = LASSCF_UnitaryGroupGenerators
    get_hop = LASSCF_HessianOperator
    def split_veff (self, veff, h2eff_sub, mo_coeff=None, ci=None, casdm1s_sub=None): 
        # This needs to actually do the veff, otherwise the preconditioner is broken
        # Eventually I can remove this, once I've implemented Schmidt decomposition etc. etc.
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if casdm1s_sub is None: casdm1s_sub = self.make_casdm1s_sub (ci = ci)
        mo_cas = mo_coeff[:,self.ncore:][:,:self.ncas]
        dm1s_cas = linalg.block_diag (*[dm[0] - dm[1] for dm in casdm1s_sub])
        dm1s = mo_cas @ dm1s_cas @ mo_cas.conj ().T
        veff_c = veff.copy ()
        veff_s = -self._scf.get_k (self.mol, dm1s, hermi=1)/2
        veff_a = veff_c + veff_s
        veff_b = veff_c - veff_s
        veff = np.stack ([veff_a, veff_b], axis=0)
        dm1s = self.make_rdm1s (mo_coeff=mo_coeff, ci=ci)
        vj, vk = self._scf.get_jk (self.mol, dm1s, hermi=1)
        veff_a = vj[0] + vj[1] - vk[0]
        veff_b = vj[0] + vj[1] - vk[1]
        veff_test = np.stack ([veff_a, veff_b], axis=0)
        assert (np.allclose (veff, veff_test))
        return veff

    def localize_init_guess (self, frags_atoms, mo_coeff=None, spin=None, lo_coeff=None, fock=None):
        ''' Here spin = 2s = number of singly-occupied orbitals '''
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        if lo_coeff is None:
            lo_coeff = orth.orth_ao (self.mol, 'meta_lowdin')
        if spin is None:
            spin = self.nelecas[0] - self.nelecas[1]
        assert (spin % 2 == sum (self.nelecas) % 2)
        assert (len (frags_atoms) == len (self.ncas_sub))
        ncore, ncas, nelecas, ncas_sub = self.ncore, self.ncas, self.nelecas, self.ncas_sub
        nocc = ncore + ncas
        mo_coeff = mo_coeff.copy ()
        nfrags = len (frags_atoms)
        nao, nmo = mo_coeff.shape
        ao_offset = self.mol.offset_ao_by_atom ()
        frags_orbs = [[orb for atom in frag_atoms for orb in list (range (ao_offset[atom,2], ao_offset[atom,3]))] for frag_atoms in frags_atoms]
        mo_core = mo_coeff[:,:ncore].copy ()
        mo_cas = mo_coeff[:,ncore:nocc].copy ()
        loc2mo = lo_coeff.conj ().T @ self._scf.get_ovlp () @ mo_cas
        cas_occ = np.zeros (ncas)
        neleca = (sum (nelecas) + spin) // 2
        nelecb = (sum (nelecas) - spin) // 2
        cas_occ[:neleca] += 1
        cas_occ[:nelecb] += 1
        null_coeff = []
        for ix, frag_orbs in enumerate (frags_orbs):
            u_frag, s, vh = linalg.svd (loc2mo[frag_orbs,:])
            u_all = np.zeros ((nao, u_frag.shape[-1]), dtype=u_frag.dtype)
            u_all[frag_orbs,:] = u_frag
            i = ncore + sum (ncas_sub[:ix])
            j = i + ncas_sub[ix]
            mo_coeff[:,i:j] = lo_coeff @ u_all[:,:ncas_sub[ix]]
            null_coeff.append (lo_coeff @ u_all[:,ncas_sub[ix]:])
        # Inactive/virtual
        null_coeff = np.concatenate (null_coeff, axis=1)
        unused_aos = np.ones (nao, dtype=np.bool_)
        for frag_orbs in frags_orbs: unused_aos[frag_orbs] = False
        assert (np.count_nonzero (unused_aos) + null_coeff.shape[-1] + ncas == nmo)
        null_coeff = np.append (null_coeff, lo_coeff[:,unused_aos], axis=1)
        ovlp = null_coeff.conj ().T @ self._scf.get_ovlp () @ mo_core
        u, s, vh = linalg.svd (ovlp)
        mo_coeff[:,:ncore] = null_coeff @ u[:,:ncore]
        mo_coeff[:,nocc:] = null_coeff @ u[:,ncore:]
        # Semi-canonicalize
        if fock is None: fock = self._scf.get_fock ()
        ranges = [(0,ncore),(nocc,nmo)]
        for ix, di in enumerate (ncas_sub):
            i = sum (ncas_sub[:ix])
            ranges.append ((i,i+di))
        for i, j in ranges:
            if (j == i): continue
            mo = mo_coeff[:,i:j]
            f = mo.conj ().T @ fock @ mo
            e, c = linalg.eigh (f)
            idx = np.argsort (e)
            mo_coeff[:,i:j] = mo @ c[:,idx]
        return mo_coeff
 
class LASSCFSymm (lasci.LASCISymm):
    get_ugg = LASSCFSymm_UnitaryGroupGenerators    
    get_hop = LASSCF_HessianOperator
    split_veff = LASSCFNoSymm.split_veff

if __name__ == '__main__':
    from pyscf import scf, lib, tools, mcscf
    import os
    class cd:
        """Context manager for changing the current working directory"""
        def __init__(self, newPath):
            self.newPath = os.path.expanduser(newPath)

        def __enter__(self):
            self.savedPath = os.getcwd()
            os.chdir(self.newPath)

        def __exit__(self, etype, value, traceback):
            os.chdir(self.savedPath)
    from mrh.tests.lasscf.me2n2_struct import structure as struct
    from mrh.my_pyscf.fci import csf_solver
    with cd ("/home/herme068/gits/mrh/tests/lasscf"):
        mol = struct (2.0, '6-31g')
    mol.output = 'lasscf_testing.log'
    mol.verbose = lib.logger.DEBUG
    mol.build ()
    mf = scf.RHF (mol).run ()
    mo_coeff = mf.mo_coeff.copy ()
    mc = mcscf.CASSCF (mf, 4, 4)
    mc.fcisolver = csf_solver (mol, smult=1)
    mc.kernel ()
    #mo_coeff = mc.mo_coeff.copy ()
    print (mc.converged, mc.e_tot)
    las = LASSCFNoSymm (mf, (4,), ((2,2),), spin_sub=(1,))
    las.kernel (mo_coeff)

    mc.mo_coeff = mo_coeff.copy ()
    las.mo_coeff = mo_coeff.copy ()
    las.ci = [[mc.ci.copy ()]]

    nao, nmo = mo_coeff.shape
    ncore, ncas = mc.ncore, mc.ncas
    nocc = ncore + ncas
    eris = mc.ao2mo ()
    from pyscf.mcscf.newton_casscf import gen_g_hop, _pack_ci_get_H
    g_all_cas, g_update_cas, h_op_cas, hdiag_all_cas = gen_g_hop (mc, mo_coeff, mc.ci, eris)
    _pack_ci, _unpack_ci = _pack_ci_get_H (mc, mo_coeff, mc.ci)[-2:]
    nvar_orb_cas = np.count_nonzero (mc.uniq_var_indices (nmo, mc.ncore, mc.ncas, mc.frozen))
    nvar_tot_cas = g_all_cas.size
    def pack_cas (kappa, ci1):
        return np.append (mc.pack_uniq_var (kappa), _pack_ci (ci1))
    def unpack_cas (x):
        return mc.unpack_uniq_var (x[:nvar_orb_cas]), _unpack_ci (x[nvar_orb_cas:])

    ugg = las.get_ugg (las, las.mo_coeff, las.ci)
    h_op_las = las.get_hop (las, ugg)

    print ("Total # variables: {} CAS ; {} LAS".format (nvar_tot_cas, ugg.nvar_tot))
    print ("# orbital variables: {} CAS ; {} LAS".format (nvar_orb_cas, ugg.nvar_orb))

    gorb_cas, gci_cas = unpack_cas (g_all_cas)
    gorb_las, gci_las = ugg.unpack (h_op_las.get_grad ())
    gorb_cas /= 2.0 # Newton-CASSCF multiplies orb-grad terms by 2 so as to exponentiate kappa instead of kappa/2

    # For orb degrees of freedom, gcas = 2 glas and therefore 2 xcas = xlas
    print (" ")
    print ("Orbital gradient norms: {} CAS ; {} LAS".format (linalg.norm (gorb_cas), linalg.norm (gorb_las)))
    print ("Orbital gradient disagreement:", linalg.norm (gorb_cas-gorb_las))
    print ("CI gradient norms: {} CAS ; {} LAS".format (linalg.norm (gci_cas), linalg.norm (gci_las)))
    print ("CI gradient disagreement:", linalg.norm (gci_cas[0]-gci_las[0][0]))
                

    np.random.seed (0)
    x = np.random.rand (ugg.nvar_tot)
    xorb, xci = ugg.unpack (x)
    def examine_sector (sector, xorb_inp, xci_inp):
        xorb = np.zeros_like (xorb_inp)
        xci = [[np.zeros_like (xci_inp[0][0])]]
        ij = {'core': (0,ncore),
              'active': (ncore,nocc),
              'virtual': (nocc,nmo)}
        if sector.upper () == 'CI':
            xci[0][0] = xci_inp[0][0].copy ()
        else:
            bra, ket = sector.split ('-')
            i, j = ij[bra]
            k, l = ij[ket]
            xorb[i:j,k:l] = xorb_inp[i:j,k:l]
            xorb[k:l,i:j] = xorb_inp[k:l,i:j]
       
        x_las = ugg.pack (xorb, xci)
        x_cas = pack_cas (xorb, xci[0]) 
        hx_orb_las, hx_ci_las = ugg.unpack (h_op_las._matvec (x_las))
        hx_orb_cas, hx_ci_cas = unpack_cas (h_op_cas (x_cas))

        hx_orb_cas /= 2.0
        if sector.upper () != 'CI':
            hx_ci_cas[0] /= 2.0 
            # g_orb (PySCF) = 2 * g_orb (LASSCF) ; hx_orb (PySCF) = 2 * hx_orb (LASSCF)
            # This means that x_orb (PySCF) = 0.5 * x_orb (LASSCF), which must have been worked into the
            # derivation of the (h_co x_o) sector of hx. Passing the same numbers into the newton_casscf
            # hx calculator will involve orbital distortions which are twice as intense as my hx 
            # hx calculator, and the newton_casscf CI sector of the hx will be twice as large

        hx_ci_cas_csf = ugg.ci_transformers[0][0].vec_det2csf (hx_ci_cas[0], normalize=False)
        hx_ci_cas[0] = ugg.ci_transformers[0][0].vec_csf2det (hx_ci_cas_csf, normalize=False)
        ci_norm = np.dot (hx_ci_cas[0].ravel (), hx_ci_cas[0].ravel ())

        print (" ")
        for osect in ('core-virtual', 'active-virtual', 'core-active'):
            bra, ket = osect.split ('-')
            i, j = ij[bra]
            k, l = ij[ket]
            print ("{} - {} Hessian sector".format (osect, sector))
            print ("Hx norms: {} CAS ; {} LAS".format (linalg.norm (hx_orb_cas[i:j,k:l]), linalg.norm (hx_orb_las[i:j,k:l])))
            print ("Hx disagreement:".format (osect, sector),
                linalg.norm (hx_orb_cas[i:j,k:l]-hx_orb_las[i:j,k:l]))
        print ("CI - {} Hessian sector".format (sector))
        print ("Hx norms: {} CAS ; {} LAS".format (linalg.norm (hx_ci_cas), linalg.norm (hx_ci_las)))
        print ("Hx disagreement:", linalg.norm (hx_ci_las[0][0]-hx_ci_cas[0]), np.dot (hx_ci_las[0][0].ravel (), hx_ci_cas[0].ravel ()) / ci_norm)

    examine_sector ('core-virtual', xorb, xci)
    examine_sector ('active-virtual', xorb, xci)
    examine_sector ('core-active', xorb, xci)
    examine_sector ('CI', xorb, xci)
    print ("\nNotes:")
    print ("1) Newton_casscf.py multiplies all orb grad terms by 2 and exponentiates by kappa as opposed to kappa/2. This is accounted for in the above.")
    print ("2) There is a known difference between my and newton_casscf's expression for H_cc x_c. I strongly believe MY version is the correct one.")
    print ("3) My preconditioner is obviously wrong, but in such a way that it suppresses CI vector evolution, which means stuff still converges if the CI vector isn't super sensitive to the orbital rotations.")

    from mrh.tests.lasscf.c2h4n4_struct import structure as struct
    mo0 = np.loadtxt ('/home/herme068/gits/mrh/tests/lasscf/test_lasci_mo.dat')
    ci00 = np.loadtxt ('/home/herme068/gits/mrh/tests/lasscf/test_lasci_ci0.dat')
    ci01 = np.loadtxt ('/home/herme068/gits/mrh/tests/lasscf/test_lasci_ci1.dat')
    ci0 = None #[[ci00], [-ci01.T]]
    dr_nn = 2.0
    mol = struct (dr_nn, dr_nn, '6-31g', symmetry=False)
    mol.verbose = lib.logger.DEBUG
    mol.output = 'lasscf_testing_c2n4h4.log'
    mol.spin = 0 
    mol.build ()
    mf = scf.RHF (mol).run ()
    mc = LASSCFNoSymm (mf, (4,4), ((2,2),(2,2)), spin_sub=(1,1))
    mo = mc.localize_init_guess ((list(range(3)),list(range(7,10))))
    tools.molden.from_mo (mol, 'localize_init_guess.molden', mo)
    mc.kernel (mo)
    tools.molden.from_mo (mol, 'c2h4n4_opt.molden', mc.mo_coeff)


