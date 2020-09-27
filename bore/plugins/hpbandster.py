import numpy as np
import tensorflow as tf

from tensorflow.keras.losses import BinaryCrossentropy
from scipy.optimize import minimize
from scipy.stats import truncnorm

from ..engine import Record
from ..models import DenseSequential
from ..optimizers import multi_start, random_start
from ..types import DenseConfigurationSpace, DenseConfiguration
from ..decorators import unbatch, value_and_gradient, numpy_io

from hpbandster.optimizers.hyperband import HyperBand
from hpbandster.core.base_config_generator import base_config_generator


minimize_multi_start = multi_start(minimizer_fn=minimize)
minimize_random_start = random_start(minimizer_fn=minimize)


class BORE(HyperBand):

    def __init__(self, config_space, eta=3, min_budget=0.01, max_budget=1,
                 gamma=None, num_random_init=10, random_rate=0.25,
                 num_start_points=10, batch_size=64, num_steps_per_iter=1000,
                 optimizer="adam", num_layers=2, num_units=32,
                 activation="relu", normalize=True, method="L-BFGS-B",
                 max_iter=100, ftol=1e-2, distortion=None, restart=False,
                 seed=None, **kwargs):

        if gamma is None:
            gamma = 1/eta

        cg = RatioEstimator(config_space=config_space, gamma=gamma,
                            num_random_init=num_random_init,
                            random_rate=random_rate,
                            num_start_points=num_start_points,
                            batch_size=batch_size,
                            num_steps_per_iter=num_steps_per_iter,
                            optimizer=optimizer, num_layers=num_layers,
                            num_units=num_units, activation=activation,
                            normalize=normalize, method=method,
                            max_iter=max_iter, ftol=ftol,
                            distortion=distortion, restart=restart, seed=seed)
        # (LT): Note this is using the *grandparent* class initializer to
        # replace the config_generator!
        super(HyperBand, self).__init__(config_generator=cg, **kwargs)

        # (LT): the design of HpBandSter framework requires us to copy-paste
        # the following boilerplate code (cannot really just subclass and
        # specify an alternative Configuration Generator).

        # Hyperband related stuff
        self.eta = eta
        self.min_budget = min_budget
        self.max_budget = max_budget

        # precompute some HB stuff
        self.max_SH_iter = -int(np.log(min_budget/max_budget)/np.log(eta)) + 1
        self.budgets = max_budget * np.power(eta, -np.linspace(self.max_SH_iter-1, 0, self.max_SH_iter))

        conf = {
            'eta': eta,
            'min_budget': min_budget,
            'max_budget': max_budget,
            'budgets': self.budgets,
            'max_SH_iter': self.max_SH_iter,
            'gamma': gamma,
            'num_random_init': num_random_init,
            'seed': seed
        }
        self.config.update(conf)


class RatioEstimator(base_config_generator):
    """
    class to implement random sampling from a ConfigSpace
    """
    def __init__(self, config_space, gamma=1/3, num_random_init=10,
                 random_rate=0.25, num_start_points=3, batch_size=64,
                 num_steps_per_iter=1000, optimizer="adam", num_layers=2,
                 num_units=32, activation="relu", normalize=True,
                 method="L-BFGS-B", max_iter=100, ftol=1e-2, distortion=None,
                 restart=False, seed=None, **kwargs):

        super(RatioEstimator, self).__init__(**kwargs)

        assert 0. < gamma < 1., "`gamma` must be in (0, 1)"
        assert 0. <= random_rate < 1., "`random_rate` must be in [0, 1)"
        assert num_random_init > 0
        assert num_start_points > 0

        self.config_space = DenseConfigurationSpace(config_space, seed=seed)
        self.bounds = self.config_space.get_bounds()

        self.logit = self._build_compile_network(num_layers, num_units,
                                                 activation, optimizer)

        if normalize:
            final = tf.sigmoid
        else:
            final = tf.identity
        self.loss = self._build_loss(activation=final)

        self.gamma = gamma
        self.num_random_init = num_random_init
        self.random_rate = random_rate

        self.num_start_points = num_start_points
        self.method = method
        self.ftol = ftol
        self.max_iter = max_iter
        self.distortion = distortion
        self.restart = restart

        self.batch_size = batch_size
        self.num_steps_per_iter = num_steps_per_iter

        self.record = Record()

        self.seed = seed
        self.random_state = np.random.RandomState(seed)

    def _array_from_dict(self, dct):
        config = DenseConfiguration(self.config_space, values=dct)
        return config.to_array()

    def _dict_from_array(self, array):
        config = DenseConfiguration.from_array(self.config_space,
                                               array_dense=array)
        return config.get_dictionary()

    def _get_steps_per_epoch(self, dataset_size):
        steps_per_epoch = int(np.ceil(np.true_divide(dataset_size,
                                                     self.batch_size)))
        return steps_per_epoch

    @staticmethod
    def _build_compile_network(num_layers, num_units, activation, optimizer):

        network = DenseSequential(output_dim=1,
                                  num_layers=num_layers,
                                  num_units=num_units,
                                  layer_kws=dict(activation=activation))
        network.compile(optimizer=optimizer, metrics=["accuracy"],
                        loss=BinaryCrossentropy(from_logits=True))
        return network

    def _build_loss(self, activation):
        """
        Returns the loss, i.e. the (negative) acquisition function to be
        minimized through the `scipy.optimize` interface.
        """
        @numpy_io
        @value_and_gradient
        @unbatch
        def loss(x):
            return - activation(self.logit(x))

        return loss

    def _update_model(self):

        X, z = self.record.load_classification_data(self.gamma)

        dataset_size = self.record.size()
        steps_per_epoch = self._get_steps_per_epoch(dataset_size)
        num_epochs = self.num_steps_per_iter // steps_per_epoch

        self.logit.fit(X, z, epochs=num_epochs, batch_size=self.batch_size,
                       verbose=False)  # TODO(LT): Make this an argument
        loss, accuracy = self.logit.evaluate(X, z, verbose=False)

        self.logger.info(f"[Model fit: loss={loss:.3f}, "
                         f"accuracy={accuracy:.3f}] "
                         f"dataset size: {dataset_size}, "
                         f"batch size: {self.batch_size}, "
                         f"steps per epoch: {steps_per_epoch}, "
                         f"num steps per iter: {self.num_steps_per_iter}, "
                         f"num epochs: {num_epochs}")

    def _get_maximum(self):

        self.logger.debug("Beginning multi-start maximization with "
                          f"{self.num_start_points} starts...")

        # TODO(LT): a lot of redundant code duplicating and confusing variable
        # naming here.
        if not self.restart:
            res = None
            i = 0
            while res is None or not (res.success or res.status == 1) \
                    or self.record.is_duplicate(res.x):
                res = minimize_random_start(self.loss, self.bounds,
                                            num_samples=self.num_start_points,
                                            method=self.method, jac=True,
                                            options=dict(maxiter=self.max_iter,
                                                         ftol=self.ftol),
                                            random_state=self.random_state)

                self.logger.debug(f"[Maximum {i+1:02d}: value={-res.fun:.3f}] "
                                  f"success: {res.success}, "
                                  f"iterations: {res.nit:02d}, "
                                  f"status: {res.status} ({res.message})")
                i += 1

            return res

        results = minimize_multi_start(self.loss, self.bounds,
                                       num_restarts=self.num_start_points,
                                       method=self.method, jac=True,
                                       options=dict(maxiter=self.max_iter,
                                                    ftol=self.ftol),
                                       random_state=self.random_state)

        res_best = None
        for i, res in enumerate(results):
            self.logger.debug(f"[Maximum {i+1:02d}/{self.num_start_points:02d}: "
                              f"value={-res.fun:.3f}] success: {res.success}, "
                              f"iterations: {res.nit:02d}, status: {res.status}"
                              f" ({res.message})")

            # TODO(LT): Create Enum type for these status codes
            # status == 1 signifies maximum iteration reached, which we don't
            # want to treat as a failure condition.
            if (res.success or res.status == 1) and \
                    not self.record.is_duplicate(res.x):
                # if (res_best is not None) *implies* (res.fun < res_best.fun)
                # (i.e. material implication) is logically equivalent to below
                if res_best is None or res.fun < res_best.fun:
                    res_best = res

        return res_best

    def get_config(self, budget):

        dataset_size = self.record.size()

        config_random = self.config_space.sample_configuration()
        config_random_dict = config_random.get_dictionary()

        if dataset_size < self.num_random_init:
            self.logger.debug(f"Completed {dataset_size}/{self.num_random_init}"
                              " initial runs. Suggesting random candidate...")
            return (config_random_dict, {})

        if self.random_state.binomial(p=self.random_rate, n=1):
            self.logger.info("[Glob. maximum: skipped "
                             f"(prob={self.random_rate:.2f})] "
                             "Suggesting random candidate ...")
            return (config_random_dict, {})

        # Update model
        self._update_model()

        # Maximize acquisition function
        opt = self._get_maximum()
        if opt is None:
            # TODO(LT): It's actually important to report what one of these
            # occurred...
            self.logger.warn("[Glob. maximum: not found!] Either optimization "
                             f"failed in all {self.num_start_points} starts, or "
                             "all maxima found have been evaluated previously!"
                             " Suggesting random candidate...")
            return (config_random_dict, {})

        loc = opt.x

        if self.distortion is None:

            config_opt_arr = loc
        else:

            # dist = multivariate_normal(mean=opt.x, cov=opt.hess_inv.todense())
            a = (self.bounds.lb - loc) / self.distortion
            b = (self.bounds.ub - loc) / self.distortion
            dist = truncnorm(a=a, b=b, loc=loc, scale=self.distortion)

            config_opt_arr = dist.rvs(random_state=self.random_state)

        self.logger.info(f"[Glob. maximum: value={-opt.fun:.3f} x={loc}] "
                         f"Suggesting x={config_opt_arr}")

        config_opt_dict = self._dict_from_array(config_opt_arr)

        return (config_opt_dict, {})

    def new_result(self, job, update_model=True):

        super(RatioEstimator, self).new_result(job)

        # TODO(LT): support multi-fidelity
        budget = job.kwargs["budget"]

        config_dict = job.kwargs["config"]
        config_arr = self._array_from_dict(config_dict)

        loss = job.result["loss"]

        self.record.append(x=config_arr, y=loss, b=budget)
