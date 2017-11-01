"""
Author: Remi Lafage <remi.lafage@onera.fr>

This package is distributed under New BSD license.

Mixture of Experts
"""

from __future__ import division
import numpy as np
import warnings
from sklearn import mixture
from sklearn import cluster
from scipy import stats as sct
from scipy.linalg import solve_triangular, cholesky

from smt.utils.options_dictionary import OptionsDictionary
from smt.extensions.extensions import Extensions
from smt.utils.misc import compute_rms_error

warnings.filterwarnings("ignore", category=DeprecationWarning)

class MOE(Extensions):
    
    def _initialize(self):
        super(MOE, self)._initialize()
        declare = self.options.declare
        
        declare('X', None, types=np.ndarray, desc='Training inputs')
        declare('y', None, types=np.ndarray, desc='Training outputs')
        declare('c', None, types=np.ndarray, desc='Clustering training outputs')
        declare('number_cluster', 1, types=int, desc='Number of cluster')
        declare('hard_recombination', True, types=bool, desc='Steep cluster transition')

    def train(self):
        super(MOE, self).apply_method()

    def predict_values(self, x):
        return super(MOE, self).analyse_results(x=x, operation='predict_values')        

    def _apply(self):
        """
        Train the MoE with 90 percent of the input samples if valid is None
        The last 10 percent are used to valid the model
        Parameters :
        ------------
        - dimension: int
        Dimension of input samples
        - x: array_like
        Input training samples
        - y:array_like
        Output training samples
        Optional :
        ------------
        - c: array_like
        Clustering training samples. Default to None. In this case, c=y.
        - valid : array_like
        Validating samples if they exist. Default to None (10%)
        - detail: Boolean
        Set True to see details of the Moe creation
        - plot: Boolean
        Set True to see comparison between real and predicted values
        - heaviside: Boolean
        Set True to optimize the heaviside factor
        - number_cluster:int
        Number of clusters. Default to 0 to find the best number of clusters
        - median:boolean
        Set True to find the best number of clusters with the median criteria.
        Set False to find the best number of clusters with means of criteria
        Default to True.
        - max_number_cluster:int
        Maximum number of clusters. Default to None (tenth of the length of x)
        - cluster_model: func
        Model used to find the best number of cluster
        """
        if self.options['X'] is not None and self.options['y'] is not None:
            x = self.options['X']
            y = self.options['y']
            c = self.options['c']
            number_cluster = self.options['number_cluster']
        else:
            raise ValueError('Check X, y')
        
        if c is None:
            c = y

        self._check_input_data(x, y, c)

        self.x = x
        self.y = y
        self.c = c

        self.scale_factor=1.
        self.hard = self.options['hard_recombination']
        dimension = x.shape[1]
        median = True
        valid = None
        max_number_cluster = None
        heaviside = False

        self.model_list = []
        self.model_list_name = []
        self.model_list_param = []

        # choice of test values and trained values
        self.values = np.c_[x, y, c]

        if valid is None:
            cut_values = cut_list(self.values, 10)  # cut space in 10 parts
            self.training_values = np.vstack(concat_except(cut_values, 0))  # assemble 9 parts
            self.test_values = np.vstack(cut_values[0])
        else:
            self.training_values = self.values
            self.test_values = valid

        total_length = self.training_values.shape[1]
        x_trained = self.training_values[:, 0:dimension]
        y_trained = self.training_values[:, dimension]
        c_trained = self.training_values[:, (dimension+1):total_length]

        # choice of number of cluster
        if max_number_cluster is None:
            max_number_cluster = int(len(x) / 10) + 1

        if number_cluster is 0:
            self._default_number_cluster(dimension, x, y, c,
                                         median=median, max_number_cluster=max_number_cluster)
        else:
            self.number_cluster = number_cluster

        if self.number_cluster > max_number_cluster:
            print 'Number of clusters should be inferior to {0}'.format(max_number_cluster)
            raise ValueError(
                'The number of clusters is too high considering the number of points')

        # Cluster space
        self.cluster = mixture.GMM(n_components=self.number_cluster,
                                   covariance_type='full', n_init=20)
        self.cluster.fit(np.c_[x_trained, c_trained])        

        # Choice of the models and training
        self._fit_without_clustering(dimension, x_trained, y_trained, c_trained)

        # choice of heaviside factor
        if heaviside and self.number_cluster > 1:
            self._best_scale_factor(
                self.test_values[:, 0:dimension], self.test_values[:, dimension], detail=detail, plot=plot)

        self.gauss = create_multivar_normal_dis(self.dimension, self.cluster.means_,
                                                    self.scale_factor * self.cluster.covars_)

        # Validation
        # xv, yv = test_values[:, 0:dimension], test_values[:, dimension]
        # ys = self._predict_smooth_output(xv)
        # yh = self._predict_hard_output(xv)
        # #self.valid_hard = self._rmse(np.atleast_2d(yv).T, yh)
        # #self.valid_smooth = self._rmse(np.atleast_2d(yv).T, ys)
        # self.valid_hard = Error(yv, yh)
        # self.valid_smooth = Error(yv, ys)
        # print "VAMID HARD", self.valid_hard 
        self._valid(self.test_values[:, 0:dimension], self.test_values[:, dimension])

        # once the validation is done, the model is trained again on all the
        # space
        if valid is None:
            self._fit_without_clustering(dimension, x, y, c, new_model=False)


    def _analyse_results(self, x, operation='predict_values', kx=None):
        if operation == 'predict_values':
            if self.hard:
                y = self._predict_hard_output(x)
            else:
                y = self._predict_smooth_output(x)
            return y 
        else:
            raise ValueError("MoE supports predict_values operation only.")
        return y
    
    def _valid(self, x, y):
        """
        Valid the Moe with the input samples
        Parameters:
        -----------
        - x: array_like
        Input testing samples
        - y : array_like
        Output testing samples
        """
        ys = self._predict_smooth_output(x)
        yh = self._predict_hard_output(x)
        self.valid_hard = Error(np.atleast_2d(y).T, yh)
        self.valid_smooth = Error(np.atleast_2d(y).T, ys)
    
    @staticmethod
    def _rmse(expected, actual):
        l_two = np.linalg.norm(expected - actual)
        l_two_rel = l_two / np.linalg.norm(expected)
        mse = (l_two**2) / len(expected)
        rmse = mse ** 0.5
        return rmse

    def _check_input_data(self, x, y, c):
        """
        Check that the data given by a user is correct
        Parameters:
        -----------
        - dimension: int
        dimension of the problem
        - x: array_like
        input points
        - y: array_like
        output points
        - c: array_like
        cluster criterion weights
        """
        if x.shape[0] != y.shape[0]:
            raise ValueError("The number of input points %d doesn t match with the number of output points %d."
                             % (x.shape[0], y.shape[0]))
        if y.shape[0] != c.shape[0]:
            raise ValueError("The number of output points %d doesn t match with the number of criterion weights %d."
                             % (y.shape[0], c.shape[0]))

    def _fit_without_clustering(self, dimension, x_trained, y_trained, c_trained, new_model=True):
        """
        Find the best model for each cluster (clustering already done) and train it if new_model is True
        Else train the points given (choice of best models by cluster already done)
        Parameters:
        -----------
        - x_trained: array_like
        Input training samples
        - y_trained: array_like
        Output training samples
        - c_trained: array_like
        Clustering training samples
        Optional:
        -----------
        - detail: Boolean
        Set True to see detail of the search
        - new_model : bool
        Set true to search the best local model
        """
        self._check_input_data(x_trained, y_trained, c_trained)

        self.dimension = dimension

        self.gauss = create_multivar_normal_dis(self.dimension, self.cluster.means_,
                                                self.scale_factor * self.cluster.covars_)

        sort_cluster = self.cluster.predict(np.c_[x_trained, c_trained])
        print(sort_cluster)

        # sort trained_values for each cluster
        trained_cluster = sort_values_by_cluster(
            np.c_[x_trained, y_trained], self.number_cluster, sort_cluster)

        # find model for each cluster
        for clus in range(self.number_cluster):

            if new_model:

                if len(self._surrogate_type) == 1:
                    pass
                    # #
                    # x_trained = np.array(trained_cluster[clus])[:, 0:dimension]
                    # y_trained = np.array(trained_cluster[clus])[:, dimension]
                    # model = self.use_model()
                    # model.set_training_values(x_trained, y_trained)
                    # model.train()
                else:
                    model = self._find_best_model(trained_cluster[clus])

                self.model_list.append(model)

            else:  # Train on the overall domain
                trained_values = np.array(trained_cluster[clus])
                x_trained = trained_values[:, 0:dimension]
                y_trained = trained_values[:, dimension]
                self.model_list[clus].set_training_values(x_trained, y_trained)
                self.model_list[clus].train()
    
    def _find_best_model(self, sorted_trained_values):
        """
        Find the best model which minimizes the errors
        Parameters :
        ------------
        - sorted_trained_values: array_like
        Training samples [[X1,X2, ..., Xn, Y], ... ]
        Optional:
        -----------
        - detail: Boolean
        Set True to see details of the search
        Returns :
        ---------
        - model : Regression_model
        Best model to apply
        - param : dictionary
        Dictionary of its parameters
        - model_name : str
        Name of the model
        """
        dimension = self.dimension
        sorted_trained_values = np.array(sorted_trained_values)
        
        rmses = {}
        sms = {}

        for name, sm_class in self._surrogate_type.iteritems():
            if name in ['RMTC', 'RMTB', 'GEKPLS', 'KRG']:
                continue
            
            sm = sm_class()
            sm.options['print_global']=False
            sm.set_training_values(sorted_trained_values[:, 0:dimension], sorted_trained_values[:, dimension])
            sm.train()
            
            expected = self.test_values[:, dimension]
            actual = sm.predict_values(self.test_values[:, 0:dimension])
            l_two = np.linalg.norm(expected - actual, 2)
            l_two_rel = l_two / np.linalg.norm(expected, 2)
            mse = (l_two**2) / len(expected)
            rmse = mse ** 0.5
            rmses[sm.name] = rmse
            print(name, rmse)
            sms[sm.name] = sm
            
        best_name=None
        best_rmse=None
        for name, rmse in rmses.iteritems():
            if best_rmse is None or rmse < best_rmse:
                best_name, best_rmse = name, rmse              
        
        print "BEST = ", best_name
        return sms[best_name]
        
    def _predict_hard_output(self, x):
        """
        This method predicts the output of a x samples for a hard recombination
        Parameters:
        ----------
        - x: Array_like
        x samples
        Return :
        ----------
        - predicted_values : array_like
        predicted output
        """
        predicted_values = []
        sort_cluster = proba_cluster(
            self.dimension, self.cluster.weights_, self.gauss, x)[1]

        for i in range(len(sort_cluster)):
            model = self.model_list[sort_cluster[i]]
            predicted_values.append(model.predict_values(np.atleast_2d(x[i]))[0])
        predicted_values = np.array(predicted_values)

        return predicted_values

    def _predict_smooth_output(self, x):
        """
        This method predicts the output of a x samples for a smooth recombination
        Parameters:
        ----------
        - x: Array_like
        x samples
        Return :
        ----------
        - predicted_values : array_like
        predicted output
        """
        predicted_values = []
        sort_proba = proba_cluster(
            self.dimension, self.cluster.weights_, self.gauss, x)[0]

        for i in range(len(sort_proba)):
            recombined_value = 0

            for j in range(len(self.model_list)):
                recombined_value = recombined_value + \
                    self.model_list[j].predict_values(np.atleast_2d(x[i]))[0] * sort_proba[i][j]

            predicted_values.append(recombined_value)

        predicted_values = np.array(predicted_values)

        return predicted_values

"""
Functions used for the clustering
"""

def sort_values_by_cluster(values, number_cluster, sort_cluster):
    """
    Sort values in each cluster
    Parameters
    ---------
    - values: array_like
    Samples to sort
    - number_cluster: int
    Number of cluster
    - sort_cluster: array_like
    Cluster corresponding to each point of value in the same order
    Returns:
    --------
    - sorted_values: array_like
    Samples sort by cluster
    Example:
    ---------
    values:
    [[  1.67016597e-01   5.42927264e-01   9.25779645e+00]
    [  5.20618344e-01   9.88223010e-01   1.51596837e+02]
    [  6.09979830e-02   2.66824984e-01   1.17890707e+02]
    [  9.62783472e-01   7.36979149e-01   7.37641826e+01]
    [  2.65769081e-01   8.09156235e-01   3.43656373e+01]
    [  8.49975570e-01   4.20496285e-01   3.48434265e+01]
    [  3.01194132e-01   8.58084068e-02   4.88696602e+01]
    [  6.40398203e-01   6.91090937e-01   8.91963162e+01]
    [  7.90710374e-01   1.40464471e-01   1.89390766e+01]
    [  4.64498124e-01   3.61009635e-01   1.04779656e+01]]

    number_cluster:
    3

    sort_cluster:
    [1 0 0 2 1 1 1 2 1 1]

    sorted_values
    [[array([   0.52061834,    0.98822301,  151.59683723]),
      array([  6.09979830e-02,   2.66824984e-01,   1.17890707e+02])]
     [array([ 0.1670166 ,  0.54292726,  9.25779645]),
      array([  0.26576908,   0.80915623,  34.36563727]),
      array([  0.84997557,   0.42049629,  34.8434265 ]),
      array([  0.30119413,   0.08580841,  48.86966023]),
      array([  0.79071037,   0.14046447,  18.93907662]),
      array([  0.46449812,   0.36100964,  10.47796563])]
     [array([  0.96278347,   0.73697915,  73.76418261]),
      array([  0.6403982 ,   0.69109094,  89.19631619])]]
    """
    sorted_values = []
    for i in range(number_cluster):
        sorted_values.append([])
    for i in range(len(sort_cluster)):
        sorted_values[sort_cluster[i]].append(values[i].tolist())
    return np.array(sorted_values)


def create_multivar_normal_dis(dim, means, cov):
    """
    Create an array of frozen multivariate normal distributions
    Parameters
    ---------
    - dim: integer
    Dimension of the problem
    - means: array_like
    Array of means
    - cov: array_like
    Array of covariances
    Returns:
    --------
    - gauss_array: array_like
    Array of frozen multivariate normal distributions with means and covariances of the input
    """
    gauss_array = []
    for k in range(len(means)):
        meansk = means[k][0:dim]
        covk = cov[k][0:dim, 0:dim]
        rv = sct.multivariate_normal(meansk, covk, True)
        gauss_array.append(rv)
    return gauss_array


def _proba_cluster_one_sample(weight, gauss_list, x):
    """
    Calculate membership probabilities to each cluster for one sample
    Parameters :
    ------------
    - weight: array_like
    Weight of each cluster
    - gauss_list : multivariate_normal object
    Array of frozen multivariate normal distributions
    - x: array_like
    The point where probabilities must be calculated
    Returns :
    ----------
    - prob: array_like
    Membership probabilities to each cluster for one input
    - clus: int
    Membership to one cluster for one input
    """
    prob = []
    rad = 0

    for k in range(len(weight)):
        rv = gauss_list[k].pdf(x)
        val = weight[k] * (rv)
        rad = rad + val
        prob.append(val)

    if rad != 0:
        for k in range(len(weight)):
            prob[k] = prob[k] / rad

    clus = prob.index(max(prob))
    return prob, clus


def _derive_proba_cluster_one_sample(weight, gauss_list, x):
    """
    Calculate the derivation term of the membership probabilities to each cluster for one sample
    Parameters :
    ------------
    - weight: array_like
    Weight of each cluster
    - gauss_list : multivariate_normal object
    Array of frozen multivariate normal distributions
    - x: array_like
    The point where probabilities must be calculated
    Returns :
    ----------
    - derive_prob: array_like
    Derivation term of the membership probabilities to each cluster for one input
    """
    derive_prob = []
    v = 0
    vprime = 0

    for k in range(len(weight)):
        v = v + weight[k] * gauss_list[k].pdf(x)
        sigma = gauss_list[k].cov
        invSigma = np.linalg.inv(sigma)
        der = np.dot((x - gauss_list[k].mean), invSigma)
        vprime = vprime - weight[k] * gauss_list[k].pdf(
            x) * der

    for k in range(len(weight)):
        u = weight[k] * gauss_list[k].pdf(
            x)
        sigma = gauss_list[k].cov
        invSigma = np.linalg.inv(sigma)
        der = np.dot((x - gauss_list[k].mean), invSigma)
        uprime = - u * der
        derive_prob.append((v * uprime - u * vprime) / (v**2))

    return derive_prob


def proba_cluster(dim, weight, gauss_list, x):
    """
    Calculate membership probabilities to each cluster for each sample
    Parameters :
    ------------
    - dimension:int
    Dimension of Input samples
    - weight: array_like
    Weight of each cluster
    - gauss_list : multivariate_normal object
    Array of frozen multivariate normal distributions
    - x: array_like
    Samples where probabilities must be calculated
    Returns :
    ----------
    - prob: array_like
    Membership probabilities to each cluster for each sample
    - clus: array_like
    Membership to one cluster for each sample
    Examples :
    ----------
    weight:
    [ 0.60103817  0.39896183]

    x:
    [[ 0.  0.]
     [ 0.  1.]
     [ 1.  0.]
     [ 1.  1.]]

    prob:
    [[  1.49050563e-02   9.85094944e-01]
     [  9.90381299e-01   9.61870088e-03]
     [  9.99208990e-01   7.91009759e-04]
     [  1.48949963e-03   9.98510500e-01]]

    clus:
    [1 0 0 1]

    """
    n = len(weight)
    prob = []
    clus = []

    for i in range(len(x)):

        if n == 1:
            prob.append([1])
            clus.append(0)

        else:
            proba, cluster = _proba_cluster_one_sample(
                weight, gauss_list, x[i])
            prob.append(proba)
            clus.append(cluster)

    return np.array(prob), np.array(clus)


def derive_proba_cluster(dim, weight, gauss_list, x):
    """
    Calculate the derivation term of the  membership probabilities to each cluster for each sample
    Parameters :
    ------------
    - dim:int
    Dimension of Input samples
    - weight: array_like
    Weight of each cluster
    - gauss_list : multivariate_normal object
    Array of frozen multivariate normal distributions
    - x: array_like
    Samples where probabilities must be calculated
    Returns :
    ----------
    - der_prob: array_like
    Derivation term of the membership probabilities to each cluster for each sample
    """
    n = len(weight)
    der_prob = []

    for i in range(len(x)):

        if n == 1:
            der_prob.append([0])

        else:
            der_proba = _derive_proba_cluster_one_sample(
                weight, gauss_list, x[i])
            der_prob.append(der_proba)

    return np.array(der_prob)

def sum_x(n):
    """
    Compute the sum from 0 to n
    Parameters:
    -----------
    - n : int
    Last integer to sum
    Return:
    -------
    Sum of the first integer until n
    """
    if n > 0:
        return n * (n + 1) / 2
    else:
        return 0

def cut_list(list_, n):
    """
    Cut a list_ in n lists with the same number of samples
    Parameters:
    -----------
    - list_ : Array_like
    The list_ to cut
    - n : int
    Number of lists needed
    Return:
    -------
    - n_list : array_like
    List of n lists
    """
    n_list = []
    for i in range(n):
        n_list.append([])
    i = 0
    length = len(list_)
    while i < length:
        j = 0
        while i < length and j < n:
            n_list[j].append(list_[i])
            i = i + 1
            j = j + 1
    return n_list


def concat_except(list_, n):
    """
    Concatenate a list_ of N lists except the n_th list
    Parameters:
    -----------
    - list_ : Array_like
    A list_ of N lists
    - n : int
    The number of the list to eliminate
    Return:
    -------
    - nlist : array_like
    The concatenated list
    """
    nlist = []
    for i in range(len(list_)):
        if i != n:
            nlist = nlist + list_[i]
    return nlist

class Error(object):
    """
    This Class contains errors :
    - l_two : float
    L2 error
    - l_two_rel : float
    relative L2 error
    - mse : float
    mse error
    - rmse : float
    rmse error
    - lof : float
    lof error
    - r_two : float
    Residual
    - err_rel: array_like
    relative errors table
    - err_rel_mean : float
    mean of err_rel
    -err_rel_max : float
    max of err_rel
    -err_abs_max: flot
    max of err_abs (norm inf)
    """

    def __init__(self, y_array_true, y_array_calc):
        length = len(y_array_true)
        self.l_two = np.linalg.norm((y_array_true - y_array_calc), 2)
        self.l_two_rel = self.l_two / np.linalg.norm((y_array_true), 2)
        self.mse = (self.l_two**2) / length
        self.rmse = self.mse ** 5
        err = np.abs((y_array_true - y_array_calc) / y_array_true)
        self.err_rel = 100 * err
        self.err_rel_mean = np.mean(self.err_rel)
        self.err_rel_max = max(self.err_rel)
        self.err_abs_max = np.linalg.norm((y_array_true - y_array_calc), np.inf)
        #self.quant = QuantError(err)
        if abs(np.var(y_array_true)) > 1e-10:
            self.lof = 100 * self.mse / np.var(y_array_true)
            self.r_two = (1 - self.lof / 100)
        else:
            self.lof = None
            self.r_two = None