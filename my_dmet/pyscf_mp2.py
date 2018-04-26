'''
    QC-DMET: a python implementation of density matrix embedding theory for ab initio quantum chemistry
    Copyright (C) 2015 Sebastian Wouters
    
    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
    
    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    
    You should have received a copy of the GNU General Public License along
    with this program; if not, write to the Free Software Foundation, Inc.,
    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
'''

import numpy as np
from pyscf import gto, scf, ao2mo, mp
from mrh.util.basis import represent_operator_in_basis
from mrh.util.rdm import get_2RDMR_from_2RDM

#def solve( CONST, OEI, FOCK, TEI, Norb, Nel, Nimp, DMguessRHF, chempot_imp=0.0, printoutput=True ):
def solve (frag, guess_1RDM, chempot_frag=0.0):

    # Augment OEI with the chemical potential
    chempot_loc = chempot_frag * np.diag (frag.is_frag_orb).astype (frag.impham_OEI.dtype)
    chempot_imp = represent_operator_in_basis (chempot_loc, frag.loc2imp)
    OEI = frag.impham_OEI - chempot_imp

    # Get the RHF solution
    mol = gto.Mole()
    mol.build( verbose=0 )
    mol.atom.append(('C', (0, 0, 0)))
    mol.nelectron = frag.nelec_imp
    mol.incore_anyway = True
    mf = scf.RHF( mol )
    mf.get_hcore = lambda *args: OEI
    mf.get_ovlp = lambda *args: np.eye( frag.norbs_imp )
    mf._eri = ao2mo.restore(8, frag.impham_TEI, frag.norbs_imp)
    mf.scf( guess_1RDM )
    DMloc = np.dot(np.dot( mf.mo_coeff, np.diag( mf.mo_occ )), mf.mo_coeff.T )
    if ( mf.converged == False ):
        mf = rhf_newtonraphson.solve( mf, dm_guess=DMloc )
    
    # Get the MP2 solution
    mp2 = mp.MP2( mf )
    mp2.kernel()
    oneRDMimp_imp  = mp2.make_rdm1()
    twoRDMRimp_imp = get_2RDMR_from_2RDM (mp2.make_rdm2 (), oneRDMimp_imp)

    # General impurity data
    frag.oneRDM_loc     = frag.oneRDMfroz_loc + represent_operator_in_basis (oneRDMimp_imp, frag.imp2loc)
    frag.twoRDMRimp_imp = twoRDMRimp_imp
    frag.E_imp          = frag.impham_CONST + mp2.e_tot + np.einsum ('ab,ab->', oneRDMimp_imp, chempot_imp)

    return None

