# This is a modification of GPflow/vgp.py by Keisuke Fujii.
#
# The original source file is distributed at
# https://github.com/GPflow/GPflow/blob/master/GPflow/svgp.py
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import absolute_import
import tensorflow as tf
import numpy as np
from GPflow.densities import gaussian
from GPflow import transforms,kullback_leiblers
from GPflow.param import AutoFlow
from GPflow.tf_wraps import eye
from GPflow._settings import settings
from .model import StVmodel
from .mean_functions import Zero
from .param import Param, DataHolder
from . import conditionals
float_type = settings.dtypes.float_type
np_float_type = np.float32 if float_type is tf.float32 else np.float64

class StVGP(StVmodel):
    """
    Stochastic approximation of the Variational Gaussian process
    """
    def __init__(self, X, Y, kern, likelihood,
                 mean_function=None, num_latent=None,
                 q_shape='fullrank',
                 KL_analytic=False,
                 num_samples=20):
        """
        X is a data matrix, size n x D
        Y is a data matrix, size n x R
        kern, likelihood, mean_function are appropriate GPflow objects
        q_shape: 'fullrank', 'diagonal', or integer less than n.
        KL_analytic: True for the use of the analytical expression for KL.
        num_samples: number of samples to approximate the posterior.
        """
        self.num_data = X.shape[0] # number of data, n
        self.num_latent = num_latent or Y.shape[1] # number of latent function, R
        self.num_samples = num_samples # number of samples to approximate integration, N
        if mean_function is None:
            mean_function = Zero(self.num_latent)
        # if minibatch_size is not None, Y is stored as MinibatchData.
        # Note that X is treated as DataHolder.
        self.Y = DataHolder(Y, on_shape_change='recompile')
        self.X = DataHolder(X, on_shape_change='recompile')
        StVmodel.__init__(self, kern, likelihood, mean_function)
        # variational parameter.
        # Mean of the posterior # shape [R,n]
        self.q_mu = Param(np.zeros((self.num_latent, self.num_data)))
        # If true, mean-field approimation is made.
        self.q_shape = q_shape
        # Sqrt of the covariance of the posterior
        # diagonal
        if self.q_shape == 'diagonal': # shape [R,n]
            self._q_sqrt = Param(np.ones((self.num_latent, self.num_data)),
                                transforms.positive)
        # fullrank
        elif self.q_shape == 'fullrank':
            q_sqrt = np.array([np.eye(self.num_data) # shape [R,n,n]
                                for _ in range(self.num_latent)])
            self._q_sqrt = Param(q_sqrt)
            # , transforms.LowerTriangular(q_sqrt.shape[2]))  # Temp remove transform                              transforms.positive)
        # multi-diagonal-case
        elif isinstance(self.q_shape, int):
            # make sure q_shape is within 1 < num_data
            assert(self.q_shape > 1 and self.q_shape < self.num_data)
            q_sqrt = np.zeros((self.num_latent, self.num_data, self.q_shape),
                                np_float_type)
            # fill one in diagonal value
            q_sqrt[:,:,self.q_shape-1] = np.ones((self.num_latent, self.num_data),np_float_type)
            self._q_sqrt = Param(q_sqrt)
        self.KL_analytic = KL_analytic

    def _compile(self, optimizer=None, **kw):
        """
        Before calling the standard compile function, check to see if the size
        of the data has changed and add variational parameters appropriately.

        This is necessary because the shape of the parameters depends on the
        shape of the data.
        """
        if not self.num_data == self.X.shape[0]:
            raise NotImplementedError
            '''
            self.num_data = self.X.shape[0]
            self.q_mu = Param(np.zeros((self.num_data, self.num_latent)))
            if self.q_diag:
                self.q_sqrt = Param(np.ones((self.num_data, self.num_latent)),
                                    transforms.positive)
            else:
                q_sqrt = np.array([np.eye(self.num_data)
                                    for _ in range(self.num_latent)]).swapaxes(0, 2)
                self.q_sqrt = Param(q_sqrt)  # , transforms.LowerTriangular(q_sqrt.shape[2]))  # Temp remove transform                              transforms.positive)
            '''
        return super(StVGP, self)._compile(optimizer=optimizer, **kw)

    def build_likelihood(self):
        """
        This method computes the variational lower bound on the likelihood, with
        stochastic approximation.
        """
        f_samples = self._sample(self.num_samples)
        # In likelihood, dimensions of f_samples and self.Y must be matched.
        lik = tf.reduce_sum(self.likelihood.logp(f_samples, self.Y))
        if not self.KL_analytic:
            return (lik - self._KL)/self.num_samples
        else:
            return lik/self.num_samples - self._analytical_KL()

    def build_predict(self, Xnew, full_cov=False):
        """
        Prediction of the latent functions.
        The posterior is approximated by multivariate Gaussian distribution.

        :param tf.tensor Xnew: Coordinate where the prediction should be made.
        :param bool full_cov: True for return full covariance.
        :return tf.tensor mean: The posterior mean sized [n,R]
        :return tf.tensor var: The posterior mean sized [n,R] for full_cov=False
                                                      [n,n,R] for full_cov=True.
        """
        if self.q_shape == 'diagonal':
            mu, var = conditionals.conditional(Xnew, self.X, self.kern, self.q_mu,
                           q_sqrt=tf.transpose(self._q_sqrt), full_cov=full_cov, whiten=True)
        else:
            mu, var = conditionals.conditional(Xnew, self.X, self.kern, self.q_mu,
                           q_sqrt=tf.transpose(self.q_sqrt,[1,2,0]), full_cov=full_cov, whiten=True)
        return mu + self.mean_function(Xnew), var


    @property
    def q_sqrt(self):
        """
        Reshape self._q_sqrt param to [R,n,n]
        """
        # Match dimension of the posterior variance to the data.
        # diagonal case
        if self.q_shape == 'diagonal':
            return tf.batch_matrix_diag(self._q_sqrt)
        else:
            if self.q_shape == 'fullrank':
                return tf.batch_matrix_band_part(self._q_sqrt, -1, 0)
            # multi-diagonal-case
            else:
                n,R,q = self.num_data, self.num_latent, self.q_shape
                # shape [R, n, q]
                paddings = [[0,0],[0,0],[n-q+1,0]]
                # shape [R, n, n+1]
                sqrt = tf.reshape(tf.slice(tf.reshape(
                                tf.pad(self._q_sqrt, paddings),  # [R,n,n+1]
                                [R, n*(n+1)]), [0,n], [R,n*n]), [R,n,n])
                             # [R,n*(n+1)] -> [R,n*n] -> [R,n,n]
            # return with [R,n,n]
                return tf.batch_matrix_band_part(sqrt, -1, 0)


    def _transform_samples(self, v):
        """
        Transform random samples picked from normal distribution v, to that from
        variational posterior u.

        u = mu + sqrt*v

        v,u: [R,n,N], mu: [R,n],
        sqrt: [R,n] for diagonal, [R,n,1] for semi-diag, [R,n,n] for fullrank
        """
        if self.q_shape == 'diagonal': # self._q_sqrt [R,n]
            return tf.expand_dims(self.q_mu, -1) + \
                   tf.expand_dims(self._q_sqrt, -1) * v
        else:
            return tf.expand_dims(self.q_mu, -1) + \
                   tf.batch_matmul(self.q_sqrt, v)
        """elif self.q_shape == 'fullrank':
            sqrt = tf.batch_matrix_band_part(self._q_sqrt, -1, 0) # [R,n,n]
            return tf.expand_dims(self.q_mu, -1) + \
                   tf.batch_matmul(sqrt, v)
        else: # semi-diag case
            n,R,q,N = self.num_data, self.num_latent, self.q_shape, self.num_samples
            sqrt = tf.expand_dims(
                    tf.batch_matrix_band_part(
                    tf.reverse(self._q_sqrt, [False,False,True]), -1, 0),2) # [R,n,1,q]
            v_pad = tf.pad(v, [[0,0],[0,1],[0,0]]) # [R,n+1,N]
            v_tile = tf.reshape(tf.slice(tf.tile(v_pad, [1,q,1]), # [R,(n+1)q,N]
                                            [0,0,0],[R,n*q,-1]),    # [R,nq, N]
                                            [R,n,q,-1])
            return tf.squeeze(tf.batch_matmul(sqrt, v_tile), [2])
        """

    def _logdet(self):
        """
        Evaluate determinant for q_sqrt
        """
        if self.q_shape == 'diagonal': # self._q_sqrt [R,n]
            return 2.0*tf.reduce_sum(tf.log(self._q_sqrt))
        elif self.q_shape == 'fullrank':
            return tf.reduce_sum(
                tf.log(tf.square(tf.batch_matrix_diag_part(self.q_sqrt))))
        else: # semi-diag
            return tf.reduce_sum(
                tf.log(tf.square(
                    tf.slice(self._q_sqrt, [0,0,self.q_shape-1], [-1,-1,-1]))))

    def _sample(self, N):
        """
        :param integer N: number of samples
        :Returns
         samples picked from the variational posterior.
         The Kulback_leibler divergence is stored as self._KL
        """
        n = self.num_data
        R = self.num_latent
        sqrt = self.q_sqrt # [R,n,n]
        # noraml random samples, [R,n,N]
        v_samples = tf.random_normal([R,n,N], dtype=float_type)
        u_samples = self._transform_samples(v_samples)
        # Stochastic approximation of the Kulback_leibler KL[q(f)||p(f)]
        self._KL = - 0.5 * self._logdet() * tf.cast(N, float_type)\
                   - 0.5 * tf.reduce_sum(tf.square(v_samples)) \
                   + 0.5 * tf.reduce_sum(tf.square(u_samples))
        # Cholesky factor of kernel [R,n,n]
        L = self.kern.Cholesky(self.X)
        # mean, sized [R,n,N]           [R,n]
        mean = tf.expand_dims(self.mean_function(self.X),-1)
        # sample from posterior, [N,n,R]
        f_samples = tf.batch_matmul(L, u_samples) + mean
        # return as Dict to deal with
        return f_samples

    def _analytical_KL(self):
        """
        Analytically evaluate KL
        """
        if self.q_shape == 'diagonal':
            KL = kullback_leiblers.gauss_kl_white_diag(self.q_mu, tf.transpose(self._q_sqrt))
        else:
            KL = kullback_leiblers.gauss_kl_white(self.q_mu, tf.transpose(self.q_sqrt, [1,2,0]))
        return KL
