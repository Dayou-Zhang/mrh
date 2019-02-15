import numpy as np
import scipy
from mrh.util import params

# A collection of simple manipulations of matrices that I somehow can't find in numpy

def is_matrix_zero (test_matrix):
    test_zero = np.zeros (test_matrix.shape, dtype=test_matrix.dtype)
    return np.allclose (test_matrix, test_zero, rtol=1)

def is_matrix_eye (test_matrix, matdim=None):
    if (test_matrix.shape[0] != test_matrix.shape[1]):
        return False
    test_eye = np.eye (test_matrix.shape[0], dtype=test_matrix.dtype)
    return np.allclose (test_matrix, test_eye, rtol=1)

def is_matrix_idempotent (test_matrix):
    if (test_matrix.shape[0] != test_matrix.shape[1]):
        return False
    test_m2 = np.dot (test_matrix, test_matrix)
    return np.allclose (test_matrix, test_m2)

def is_matrix_diagonal (test_matrix):
    test_diagonal = np.diag (np.diag (test_matrix))
    return np.allclose (test_matrix, test_diagonal)

def is_matrix_hermitian (test_matrix):
    test_adjoint = np.transpose (np.conjugate (test_matrix))
    return np.allclose (test_matrix, test_adjoint)

def assert_matrix_square (test_matrix, matdim=None):
    if (matdim == None):
        matdim = test_matrix.shape[0]
    assert ((test_matrix.ndim == 2) and (test_matrix.shape[0] == matdim) and (test_matrix.shape[1] == matdim)), "Matrix shape is {0}; should be ({1},{1})".format (test_matrix.shape, matdim)
    return matdim

def matrix_svd_control_options (the_matrix, full_matrices=False, sort_vecs=-1, only_nonzero_vals=False, num_zero_atol=params.num_zero_atol):
    if the_matrix.shape == tuple((0,0)):
        return np.zeros ((0)), np.zeros ((0,0))
    pMq = np.asarray (the_matrix)
    lvecs_pl, svals_lr, rvecs_rq = scipy.linalg.svd (np.asarray (the_matrix), full_matrices=full_matrices)
    p2l = lvecs_pl
    r2q = rvecs_rq
    q2r = r2q.conjugate ().T
    if sort_vecs:
        idx_sval = (np.abs (svals_lr)).argsort ()[::sort_vecs]
        idx_q2r = np.append (idx_sval, np.arange (len (idx_sval), q2r.shape[1], dtype=idx_sval.dtype))
        idx_p2l = np.append (idx_sval, np.arange (len (idx_sval), p2l.shape[1], dtype=idx_sval.dtype))
        svals_lr = svals_lr[idx_sval]
        q2r = q2r[:,idx_q2r]
        p2l = p2l[:,idx_p2l]
    if only_nonzero_vals:
        idx = np.where (np.abs (svals_lr) > num_zero_atol)[0]
        svals_lr = svals_lr[idx]
        q2r = q2r[:,idx]
        p2l = p2l[:,idx]

    lvecs, svals_lr, rvecs = (np.asarray (output) for output in (p2l, svals_lr, q2r))
    return lvecs, svals_lr, rvecs

def matrix_eigen_control_options (the_matrix, symm_blocks=None, sort_vecs=-1, only_nonzero_vals=False, round_zero_vals=False, b_matrix=None, num_zero_atol=params.num_zero_atol):
    if the_matrix.shape == tuple((0,0)):
        return np.zeros ((0)), np.zeros ((0,0))
    # Use recursion to make this work
    # This must assume that bra and ket bases are the same... if they aren't I should be doing SVD anyway
    if symm_blocks is not None:
        # If you gave me a list of basis spaces:
        if isinstance (symm_blocks[0], np.ndarray):
            symm_umat = np.concatenate (symm_blocks, axis=1)
            assert (symm_umat.shape == the_matrix.shape), "I can't guess how to map symmetry blocks to different bases!"
            symm_lbls = np.concatenate ([idx * np.ones (blk.shape[1]) for idx, blk in enumerate (symm_blocks)])
            assert (not isinstance (symm_lbls[0], np.ndarray)), '? {}'.format (symm_lbls)
            symm_matr = symm_umat.conjugate ().T @ the_matrix @ symm_umat
            evals, symm_evecs, labels = matrix_eigen_control_options (symm_matr, symm_blocks=symm_lbls,
                sort_vecs=sort_vecs, only_nonzero_vals=only_nonzero_vals, round_zero_vals=round_zero_vals,
                b_matrix=b_matrix, num_zero_atol=num_zero_atol)
            evecs = symm_umat @ symm_evecs
            return evals, evecs, labels
        # If you gave me a list of integers or irrep symbols:
        uniq_lbls = np.unique (symm_blocks)
        evals = []
        evecs = []
        labels = []
        for lbl in uniq_lbls:
            idx_blk = np.ix_(symm_blocks == lbl, symm_blocks == lbl)
            mat_blk = the_matrix[idx_blk]
            evals_blk, _evecs_blk = matrix_eigen_control_options (mat_blk, symm_blocks=None,
                sort_vecs=sort_vecs, only_nonzero_vals=only_nonzero_vals, round_zero_vals=round_zero_vals,
                b_matrix=b_matrix, num_zero_atol=num_zero_atol)
            evecs_blk = np.zeros ((the_matrix.shape[0], _evecs_blk.shape[1]), dtype=_evecs_blk.dtype)
            evecs_blk[symm_blocks == lbl,:] = _evecs_blk
            evals.append (evals_blk)
            evecs.append (evecs_blk)
            labels.extend ([lbl for ix in range (len (evals_blk))])
        evals = np.concatenate (evals)
        evecs = np.concatenate (evecs, axis=1)
        labels = np.asarray (labels)
        if sort_vecs:
            idx = evals.argsort ()[::sort_vecs]
            evals = evals[idx]
            evecs = evecs[:,idx]
            labels = labels[idx]
        return evals, evecs, labels

    # Now for the actual damn kernel            
    # Subtract a diagonal average from the matrix to fight rounding error
    diag_avg = np.eye (the_matrix.shape[0]) * np.mean (np.diag (the_matrix))
    pMq = np.asmatrix (the_matrix - diag_avg)
    qSr = None if b_matrix is None else np.asmatrix (b_matrix)
    # Use hermitian diagonalizer if possible and don't do anything if the matrix is already diagonal
    evals = np.diagonal (pMq)
    evecs = np.asmatrix (np.eye (len (evals), dtype=evals.dtype))
    if not is_matrix_diagonal (pMq):
        evals, evecs = scipy.linalg.eigh (pMq, qSr) if is_matrix_hermitian (pMq) else scipy.linalg.eig (pMq, qSr)
    # Add the diagonal average to the eigenvalues when returning!
    evals = evals + np.diag (diag_avg)
    if only_nonzero_vals:
        idx = np.where (np.abs (evals) > num_zero_atol)[0]
        evals = evals[idx]
        evecs = evecs[:,idx]
    if sort_vecs:
        idx = evals.argsort ()[::sort_vecs]
        evals = evals[idx]
        evecs = evecs[:,idx]
    if round_zero_vals:
        idx = np.where (np.abs (evals) < num_zero_atol)[0]
        evals[idx] = 0
    evals, evecs = (np.asarray (output) for output in (evals, evecs))
    return evals, evecs



