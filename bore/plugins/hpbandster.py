import numpy as np
import tensorflow as tf
import tensorflow.keras.backend as K

from tensorflow.keras.losses import BinaryCrossentropy
# from tensorflow.keras.callbacks import EarlyStopping
from scipy.optimize import minimize

from ..data import Record
from ..engine import truncated_normal
from ..models import DenseMaximizableSequential
from ..optimizers import multi_start
from ..types import DenseConfigurationSpace, DenseConfiguration

from hpbandster.optimizers.hyperband import HyperBand
from hpbandster.core.base_config_generator import base_config_generator


TRANSFORMS = dict(identity=tf.identity, sigmoid=tf.sigmoid, exp=tf.exp)

minimize_multi_start = multi_start(minimizer_fn=minimize)


def maybe_distort(loc, distortion=None, bounds=None, random_state=None,
                  print_fn=print):

    if distortion is None:
        return loc

    assert bounds is not None, "must specify bounds!"
    ret = truncated_normal(loc=loc,
                           scale=distortion,
                           lower=bounds.lb,
                           upper=bounds.ub).rvs(random_state=random_state)
    print_fn(f"Suggesting x={ret} (after applying distortion={distortion:.3E})")

    return ret


class BORE(HyperBand):

    def __init__(self, config_space, eta=3, min_budget=0.01, max_budget=1,
                 gamma=None, num_random_init=10, random_rate=None, retrain=False,
                 num_starts=5, num_samples=512, batch_size=64,
                 num_steps_per_iter=1000, num_epochs=None, optimizer="adam",
                 num_layers=2, num_units=32, activation="relu",
                 transform="sigmoid", method="L-BFGS-B", max_iter=100,
                 ftol=1e-2, distortion=None, restart=False, seed=None, **kwargs):

        if gamma is None:
            gamma = 1/eta

        cg = RatioEstimator(config_space=config_space, gamma=gamma,
                            num_random_init=num_random_init,
                            random_rate=random_rate, retrain=retrain,
                            classifier_kws=dict(num_layers=num_layers,
                                                num_units=num_units,
                                                activation=activation,
                                                optimizer=optimizer),
                            fit_kws=dict(batch_size=batch_size,
                                         num_steps_per_iter=num_steps_per_iter,
                                         num_epochs=num_epochs),
                            optimizer_kws=dict(transform=transform,
                                               method=method,
                                               max_iter=max_iter,
                                               ftol=ftol,
                                               distortion=distortion,
                                               num_starts=num_starts,
                                               num_samples=num_samples),
                            seed=seed)
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
    def __init__(self, config_space, gamma, num_random_init, random_rate,
                 retrain, classifier_kws, fit_kws, optimizer_kws, seed, **kwargs):

        super(RatioEstimator, self).__init__(**kwargs)

        assert 0. < gamma < 1., "`gamma` must be in (0, 1)"
        assert num_random_init > 0
        assert random_rate is None or 0. <= random_rate < 1., \
            "`random_rate` must be in [0, 1)"

        self.gamma = gamma
        self.num_random_init = num_random_init
        self.random_rate = random_rate

        # Build ConfigSpace with one-hot-encoded categorical inputs and
        # initialize bounds
        self.config_space = DenseConfigurationSpace(config_space, seed=seed)
        self.bounds = self.config_space.get_bounds()

        self.input_dim = self.config_space.get_dimensions(sparse=False)

        self.num_layers = classifier_kws.get("num_layers", 2)
        self.num_units = classifier_kws.get("num_units", 32)
        self.activation = classifier_kws.get("activation", "relu")
        self.optimizer = classifier_kws.get("optimizer", "adam")

        self.retrain = retrain
        self.logit = None

        # Options for fitting neural network parameters
        self.batch_size = fit_kws.get("batch_size", 64)
        self.num_steps_per_iter = fit_kws.get("num_steps_per_iter", 100)
        self.num_epochs = fit_kws.get("num_epochs")

        # Options for maximizing the acquisition function
        transform_name = optimizer_kws.get("transform", "sigmoid")
        assert transform_name in TRANSFORMS, \
            f"`transform` must be one of {tuple(TRANSFORMS.keys())}"
        self.transform = TRANSFORMS.get(transform_name)

        assert optimizer_kws.get("num_starts") > 0
        self.num_starts = optimizer_kws.get("num_starts", 5)
        self.num_samples = optimizer_kws.get("num_samples", 512)
        self.method = optimizer_kws.get("method", "L-BFGS-B")
        self.ftol = optimizer_kws.get("ftol", 1e-2)
        self.max_iter = optimizer_kws.get("max_iter", 100)
        self.distortion = optimizer_kws.get("distortion")

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

    def _build_compile_network(self):

        self.logger.debug("Building and compiling network...")
        network = DenseMaximizableSequential(transform=self.transform,
                                             input_dim=self.input_dim,
                                             output_dim=1,
                                             num_layers=self.num_layers,
                                             num_units=self.num_units,
                                             layer_kws=dict(
                                                activation=self.activation))
        network.compile(optimizer=self.optimizer, metrics=["accuracy"],
                        loss=BinaryCrossentropy(from_logits=True))
        network.summary(print_fn=self.logger.debug)

        return network

    def _update_classifier(self):

        X, z = self.record.load_classification_data(self.gamma)

        dataset_size = self.record.size()
        steps_per_epoch = self._get_steps_per_epoch(dataset_size)

        num_epochs = self.num_epochs
        if num_epochs is None:
            num_epochs = self.num_steps_per_iter // steps_per_epoch
            self.logger.debug("Argument `num_epochs` has not been specified. "
                              f"Setting num_epochs={num_epochs}")
        else:
            self.logger.debug("Argument `num_epochs` is specified "
                              f"(num_epochs={num_epochs}). "
                              f"Ignoring num_steps_per_iter={self.num_steps_per_iter}")

        callbacks = []
        # TODO(LT): Add option
        # early_stopping = EarlyStopping(monitor="loss", min_delta=1e-3,
        #                                verbose=True, patience=5, mode="min")
        # callbacks.append(early_stopping)

        self.logit.fit(X, z, epochs=num_epochs, batch_size=self.batch_size,
                       callbacks=callbacks, verbose=False)  # TODO(LT): Make this an argument
        loss, accuracy = self.logit.evaluate(X, z, verbose=False)

        self.logger.info(f"[Model fit: loss={loss:.3f}, "
                         f"accuracy={accuracy:.3f}] "
                         f"dataset size: {dataset_size}, "
                         f"batch size: {self.batch_size}, "
                         f"steps per epoch: {steps_per_epoch}, "
                         f"num steps per iter: {self.num_steps_per_iter}, "
                         f"num epochs: {num_epochs}")

    def _maybe_create_classifier(self):
        # Build neural network probabilistic classifier
        if self.logit is None:
            self.logit = self._build_compile_network()

    def _maybe_delete_classifier(self):
        if self.retrain:
            # if we are not persisting model across optimization iterations
            # delete and clear from memory
            self.logger.debug("Deleting model...")
            K.clear_session()
            del self.logit
            self.logit = None  # reset

    def _is_unique(self, res):
        is_duplicate = self.record.is_duplicate(res.x)
        if is_duplicate:
            self.logger.warn("Duplicate detected! Skipping...")
        return not is_duplicate

    def get_config(self, budget):

        dataset_size = self.record.size()

        config_random = self.config_space.sample_configuration()
        config_random_dict = config_random.get_dictionary()

        # epsilon-greedy exploration
        if self.random_rate is not None and \
                self.random_state.binomial(p=self.random_rate, n=1):
            self.logger.info("[Glob. maximum: skipped "
                             f"(prob={self.random_rate:.2f})] "
                             "Suggesting random candidate ...")
            return (config_random_dict, {})

        # Insufficient training data
        if dataset_size < self.num_random_init:
            self.logger.debug(f"Completed {dataset_size}/{self.num_random_init}"
                              " initial runs. Suggesting random candidate...")
            return (config_random_dict, {})

        # Create classifier (if retraining from scratch every iteration)
        self._maybe_create_classifier()

        # Train classifier
        self._update_classifier()

        # Maximize classifier wrt input
        self.logger.debug("Beginning multi-start maximization with "
                          f"{self.num_starts} starts...")
        opt = self.logit.argmax(self.bounds,
                                num_starts=self.num_starts,
                                num_samples=self.num_samples,
                                method=self.method,
                                options=dict(maxiter=self.max_iter,
                                             ftol=self.ftol),
                                print_fn=self.logger.debug,
                                filter_fn=self._is_unique,
                                random_state=self.random_state)
        if opt is None:
            # TODO(LT): It's actually important to report which of these
            # failures occurred...
            self.logger.warn("[Glob. maximum: not found!] Either optimization "
                             f"failed in all {self.num_starts} starts, or "
                             "all maxima found have been evaluated previously!"
                             " Suggesting random candidate...")
            return (config_random_dict, {})

        loc = opt.x
        self.logger.info(f"[Glob. maximum: value={-opt.fun:.3f} x={loc}]")
        config_opt_arr = maybe_distort(loc, self.distortion,
                                       self.bounds, self.random_state,
                                       print_fn=self.logger.info)
        config_opt_dict = self._dict_from_array(config_opt_arr)

        # Delete classifier (if retraining from scratch every iteration)
        self._maybe_delete_classifier()

        return (config_opt_dict, {})

    def new_result(self, job, update_model=True):

        super(RatioEstimator, self).new_result(job)

        # TODO(LT): support multi-fidelity
        budget = job.kwargs["budget"]

        config_dict = job.kwargs["config"]
        config_arr = self._array_from_dict(config_dict)

        loss = job.result["loss"]

        self.record.append(x=config_arr, y=loss, b=budget)
