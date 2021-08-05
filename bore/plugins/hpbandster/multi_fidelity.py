import numpy as np
# import tensorflow as tf
import tensorflow.keras.backend as K

from tensorflow.keras.losses import BinaryCrossentropy
from tensorflow.keras.regularizers import l2

from hpbandster.optimizers.hyperband import HyperBand
from hpbandster.core.base_config_generator import base_config_generator

from .types import DenseConfigurationSpace, array_from_dict, dict_from_array
from .base import TRANSFORMS
from ...base import maybe_distort
from ...math import steps_per_epoch
from ...data import MultiFidelityRecord
from ...models import StackedRecurrentFactory


class BOREHyperband(HyperBand):

    def __init__(self, config_space, eta=3, min_budget=0.01, max_budget=1,
                 gamma=None, num_random_init=10, random_rate=0.1, retrain=False,
                 num_starts=5, num_samples=1024, batch_size=64,
                 num_steps_per_iter=1000, num_epochs=None, optimizer="adam",
                 mask_value=-1.,
                 num_layers=2, num_units=32, activation="elu", l2_factor=None,
                 transform="sigmoid", method="L-BFGS-B", max_iter=1000,
                 ftol=1e-9, distortion=None, seed=None, **kwargs):

        if gamma is None:
            gamma = 1/eta

        cg = SequenceClassifierConfigGenerator(config_space=config_space,
                                               gamma=gamma,
                                               num_random_init=num_random_init,
                                               random_rate=random_rate,
                                               retrain=retrain,
                                               classifier_kws=dict(
                                                num_layers=num_layers,
                                                num_units=num_units,
                                                l2_factor=l2_factor,
                                                activation=activation,
                                                optimizer=optimizer,
                                                mask_value=mask_value),
                                               fit_kws=dict(
                                                batch_size=batch_size,
                                                num_steps_per_iter=num_steps_per_iter,
                                                num_epochs=num_epochs),
                                               optimizer_kws=dict(
                                                transform=transform,
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


class SequenceClassifierConfigGenerator(base_config_generator):

    def __init__(self, config_space, gamma, num_random_init, random_rate,
                 retrain, classifier_kws, fit_kws, optimizer_kws, seed, **kwargs):

        super(SequenceClassifierConfigGenerator, self).__init__(**kwargs)

        assert 0. < gamma < 1., "`gamma` must be in (0, 1)"
        assert num_random_init > 0, "number of initial random designs " \
            "must be non-zero!"
        assert random_rate is None or 0. <= random_rate < 1., \
            "`random_rate` must be in [0, 1)"

        self.gamma = gamma
        self.num_random_init = num_random_init
        self.random_rate = random_rate

        # Build ConfigSpace with one-hot-encoded categorical inputs and
        # initialize bounds
        self.config_space = DenseConfigurationSpace(config_space, seed=seed)

        self.input_dim = self.config_space.get_dimensions(sparse=False)
        self.bounds = self.config_space.get_bounds()

        self.optimizer = classifier_kws.get("optimizer", "adam")
        self.mask_value = classifier_kws.get("mask_value", 1e-9)

        num_layers = classifier_kws.get("num_layers", 2)
        num_units = classifier_kws.get("num_units", 32)
        activation = classifier_kws.get("activation", "elu")

        l2_factor = classifier_kws.get("l2_factor")

        kernel_regularizer = None if l2_factor is None else l2(l2_factor)
        bias_regularizer = None if l2_factor is None else l2(l2_factor)

        self.model_factory = StackedRecurrentFactory(
            input_dim=self.input_dim,
            output_dim=1,
            num_layers=num_layers,
            num_units=num_units,
            layer_kws=dict(
                activation=activation,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer
            )
        )

        if retrain:
            raise NotImplementedError
        self.retrain = retrain

        self.logit = self._build_compile_network()
        self.funcs = {}

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
        self.num_samples = optimizer_kws.get("num_samples", 1024)
        self.method = optimizer_kws.get("method", "L-BFGS-B")
        self.ftol = optimizer_kws.get("ftol", 1e-9)
        self.max_iter = optimizer_kws.get("max_iter", 1000)
        self.distortion = optimizer_kws.get("distortion")

        self.record = MultiFidelityRecord(gamma=gamma)

        self.seed = seed
        self.random_state = np.random.RandomState(seed)

    def _build_compile_network(self):
        self.logger.debug("Building and compiling network...")
        network = self.model_factory.build_many_to_many(mask_value=self.mask_value)
        network.compile(optimizer=self.optimizer, metrics=["accuracy"],
                        loss=BinaryCrossentropy(from_logits=True))
        network.summary(print_fn=self.logger.debug)
        return network

    # def _maybe_create_classifier(self):
    #     # Build neural network probabilistic classifier
    #     if self.logit is None:
    #         self.model_factory = StackedRecurrentFactory(
    #             input_dim=self.input_dim,
    #             output_dim=1,
    #             num_layers=self.num_layers,
    #             num_units=self.num_units,
    #             layer_kws=dict(activation=self.activation,
    #                            kernel_regularizer=self.kernel_regularizer,
    #                            bias_regularizer=self.bias_regularizer))
    #         self.logit = self._build_compile_network()

    # def _maybe_delete_classifier(self):
    #     if self.retrain:
    #         # if we are not persisting model across optimization iterations
    #         # delete and clear from memory
    #         self.logger.debug("Deleting model...")
    #         K.clear_session()
    #         del self.logit
    #         self.logit = None  # reset

    def _update_classifier(self):

        inputs, targets = self.record.sequences_padded(binary=True,
                                                       pad_value=self.mask_value)
        self.logger.debug(f"Input sequence shape: {inputs.shape}")
        self.logger.debug(f"Target sequence shape: {targets.shape}")

        dataset_size = self.record.num_features()
        num_steps = steps_per_epoch(dataset_size, self.batch_size)

        num_epochs = self.num_epochs
        if num_epochs is None:
            num_epochs = self.num_steps_per_iter // num_steps
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

        self.logit.fit(inputs, targets, epochs=num_epochs,
                       batch_size=self.batch_size, callbacks=callbacks,
                       verbose=False)
        loss, accuracy = self.logit.evaluate(inputs, targets, verbose=False)

        self.logger.info(f"[Model fit: loss={loss:.3f}, "
                         f"accuracy={accuracy:.3f}] "
                         # f"dataset size: {dataset_size}, "
                         f"batch size: {self.batch_size}, "
                         # f"steps per epoch: {num_steps}, "
                         f"num steps per iter: {self.num_steps_per_iter}, "
                         f"num epochs: {num_epochs}")

    def _is_unique(self, res):
        is_duplicate = self.record.is_duplicate(res.x)
        if is_duplicate:
            self.logger.warn("Duplicate detected! Skipping...")
        return not is_duplicate

    def get_config(self, budget):

        config_random = self.config_space.sample_configuration()
        config_random_dict = config_random.get_dictionary()

        # epsilon-greedy exploration
        if self.random_rate is not None and \
                self.random_state.binomial(p=self.random_rate, n=1):
            self.logger.info("[Glob. maximum: skipped "
                             f"(prob={self.random_rate:.2f})] "
                             "Suggesting random candidate ...")
            return (config_random_dict, {})

        # TODO(LT): Should just skip based on number of unique input features
        # observed so far.
        # Insufficient training data
        t = self.record.highest_rung(min_size=self.num_random_init)
        if t is None:
            self.logger.debug("There are no rungs with at least "
                              f"{self.num_random_init} observations. "
                              "Suggesting random candidate...")
            return (config_random_dict, {})

        self.logger.debug(f"Rung {t} is the highest with at least "
                          f"{self.num_random_init} observations.")

        # # Create classifier (if retraining from scratch every iteration)
        # self._maybe_create_classifier()

        # Train classifier
        self._update_classifier()

        # Classifier specific to a rung. These are built on-the-fly and then
        # cached for use in subsquent iterations.
        num_steps = t + 1  # rungs are zero-based
        func = self.funcs.setdefault(t, self.model_factory.build_one_to_one(num_steps, transform=self.transform))

        # Maximize classifier wrt input
        self.logger.debug("Beginning multi-start maximization with "
                          f"{self.num_starts} starts...")
        opt = func.argmax(self.bounds,
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
        config_opt_dict = dict_from_array(self.config_space, config_opt_arr)

        # # Delete classifier (if retraining from scratch every iteration)
        # self._maybe_delete_classifier()

        return (config_opt_dict, {})

    def new_result(self, job, update_model=True):

        super(SequenceClassifierConfigGenerator, self).new_result(job)

        budget = job.kwargs["budget"]

        config_dict = job.kwargs["config"]
        config_arr = array_from_dict(self.config_space, config_dict)

        loss = job.result["loss"]

        self.record.append(x=config_arr, y=loss, b=budget)

        self.logger.debug(f"[Data] rungs: {self.record.num_rungs()}, "
                          f"budgets: {self.record.budgets()}, "
                          f"rung sizes: {self.record.rung_sizes()}")
        self.logger.debug(f"[Data] thresholds: {self.record.thresholds()}")
