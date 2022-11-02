"""
PhysioFit software main module
"""

import logging

import numpy as np
from pandas import DataFrame
from scipy.optimize import minimize, differential_evolution
from scipy.stats import chi2

from physiofit.logger import initialize_fitter_logger
from physiofit.models.base_model import Model

mod_logger = logging.getLogger("PhysioFit.base.fitter")


# TODO: add estimate deg function (eq 6) with plot of best fit and measured values


class PhysioFitter:
    """
    This class is responsible for most of Physiofit's heavy lifting. Features included are:

        * loading of data from **csv** or **tsv** file
        * **equation system initialization** using the following analytical functions (in absence of lag and
          degradation:

            X(t) = X0 * exp(mu * t)
            Mi(t) = qMi * (X0 / mu) * (exp(mu * t) - 1) + Mi0

        * **simulation of data points** from given initial parameters
        * **cost calculation** using the equation:

            residuum = sum((sim - meas) / sd)²

        * **optimization of the initial parameters** using `scipy.optimize.minimize ('Differential evolution', with polish with 'L-BFGS-B' method) <https://docs.scipy.org/doc/scipy/reference/optimize.minimize-lbfgsb.html#optimize-minimize-lbfgsb>`_
        * **sensitivity analysis, khi2 tests and plotting**

    :param data: DataFrame containing data and passed by IOstream object
    :type data: class: pandas.DataFrame
    :param mc: Should Monte-Carlo sensitivity analysis be performed (default=True)
    :type mc: Boolean
    :param iterations: number of iterations for Monte-Carlo simulation (default=50)
    :type iterations: int
    :param sd: sd matrix used for residuum calculations. Can be:

                * a matrix with the same dimensions as the measurements matrix (but without the time column)
                * a named vector containing sds for all the metabolites provided in the input file
                * 0  in which case the matrix is automatically constructed from default values
                * a dictionary with the data column headers as keys and the associated value as a scalar or list

    :type sd: int, float, list, dict or ndarray
    :param deg: dictionary of degradation constants for each metabolite
    :type deg: dict
    :param t_lag: Should lag phase length be estimated
    :type t_lag: bool
    """

    def __init__(
            self, data, mc=True, iterations=100,
            sd=None, debug_mode=False
    ):

        self.data = data
        self.mc = mc
        self.iterations = iterations
        self.sd = sd
        self.debug_mode = debug_mode
        if not hasattr(self, "logger"):
            self.logger = initialize_fitter_logger(self.debug_mode)

        # Initialize model
        self.model = Model(self.data)
        self.model.get_params()

        self.simulated_matrix = None
        self.simulated_data = None
        self.optimize_results = None
        self.simulate = None
        self.time_vector = None
        self.name_vector = None
        self.deg_vector = None
        self.experimental_matrix = None
        self.params = None
        self.ids = None
        self.bounds = None
        self.parameter_stats = None
        self.opt_params_sds = None
        self.matrices_ci = None
        self.opt_conf_ints = None
        self.khi2_res = None

    # def verify_attrs(self):
    #     """Check that attributes are valid"""
    #
    #     allowed_vinis = [int, float]
    #     if type(self.vini) not in allowed_vinis:
    #         raise TypeError(f"Initial value for fluxes and concentrations is not a number. Detected type:  "
    #                         f"{type(self.vini)}\nValid types: {allowed_vinis}")
    #
    #     for bound in [self.conc_biom_bounds, self.flux_biom_bounds, self.conc_met_bounds, self.flux_met_bounds]:
    #         if type(bound) is not tuple:
    #             raise TypeError(f"Error reading bounds. Bounds should be ('int/float', 'int/float') tuples.\n"
    #                             f"Current bounds: {bound}")
    #         if self.vini < bound[0] or self.vini > bound[1]:
    #             raise RuntimeError(f"Initial value for fluxes and concentrations ({self.vini}) cannot be set outside "
    #                                f"the given bounds: {bound}")
    #
    #     if type(self.iterations) is not int:
    #         raise TypeError(f"Number of monte carlo iterations must be an integer, and not of type "
    #                         f"{type(self.iterations)}")
    #
    #     allowed_sds = [int, float, list, np.ndarray]
    #     if type(self.sd) not in allowed_sds:
    #         raise TypeError(f"sds is not in the right format ({type(self.sd)}. "
    #                         f"Compatible formats are:\n{allowed_sds}")
    #
    #     if type(self.deg_vector) is not list:
    #         raise TypeError(f"Degradation constants have not been well initialized.\nConstants: {self.deg}")
    #
    #     if type(self.t_lag) is not bool:
    #         raise TypeError(f"t_lag parameter must be a boolean (True or False)")

    def _sd_dict_to_matrix(self):
        """Convert sd dictionary to matrix/vector"""

        # Perform checks
        for key in self.sd.keys():
            if key not in self.model.name_vector:
                raise KeyError(f"The key {key} is not part of the data headers")
        for name in self.model.name_vector:
            if name not in self.sd.keys():
                raise KeyError(f"The key {name} is missing from the sds dict")

        # Get lengths of each sd entry
        sd_lengths = [
            len(self.sd[key]) if type(self.sd[key]) not in [float, int] else 1
            for key in self.sd.keys()
        ]

        # Make sure that lengths are the same
        if not all(elem == sd_lengths[0] for elem in sd_lengths):
            raise ValueError("All sd vectors must have the same length")

        # Build matrix/vector
        if sd_lengths[0] == 1:
            self.sd = [self.sd[name] for name in self.model.name_vector]
        else:
            columns = (self.sd[name] for name in self.model.name_vector)
            matrix = np.column_stack(columns)
            self.sd = matrix

    def initialize_sd_matrix(self):
        """
        Initialize the sd matrix from different types of inputs: single value,
        vector or matrix.

        :return: None
        """

        # This function can be optimized, if the input is a matrix we should
        # detect it directly
        self.logger.info("Initializing sd matrix...\n")

        # If sd is None, we generate the default matrix
        if self.sd is None:
            try:
                self.sd = {"X": 0.2}
                for col in self.data.columns[2:]:
                    self.sd.update({col: 0.5})
            except Exception:
                raise

        if isinstance(self.sd, dict):
            self._sd_dict_to_matrix()
        # When sd is a single value, we build a sd matrix containing the value
        # in all positions
        if isinstance(self.sd, int) or isinstance(self.sd, float):
            self._build_sd_matrix()
            self.logger.debug(f"SD matrix: {self.sd}\n")
            return
        if not isinstance(self.sd, np.ndarray) and not isinstance(self.sd, list):
            raise TypeError(
                f"Cannot coerce SD to array. Please check that a list or array "
                f"is given as input.\nCurrent input: \n{self.sd}"
            )
        else:
            self.sd = np.array(self.sd)
        if not np.issubdtype(self.sd.dtype, np.number):
            try:
                self.sd = self.sd.astype(float)
            except ValueError:
                raise ValueError(
                    f"The sd vector/matrix contains values that are not "
                    f"numeric. \nCurrent sd vector/matrix: \n{self.sd}"
                )
            except Exception as e:
                raise RuntimeError(f"Unknown error: {e}")
        else:
            # If the array is not the right shape, we assume it is a vector
            # that needs to be tiled into a matrix
            if self.sd.shape != self.experimental_matrix.shape:
                try:
                    self._build_sd_matrix()
                except ValueError:
                    raise
                except RuntimeError:
                    raise
            else:
                self.logger.debug(f"sd matrix: {self.sd}\n")
                return
        self.logger.info(f"sd Matrix:\n{self.sd}\n")

    def _build_sd_matrix(self):
        """
        Build the sd matrix from different input types

        :return: None
        """

        # First condition: the sds are in a 1D array
        if isinstance(self.sd, np.ndarray):
            # We first check that the sd vector is as long as the
            # experimental matrix on the row axis
            if self.sd.size != self.experimental_matrix[0].size:
                raise ValueError("sd vector not of right size")
            else:
                # We duplicate the vector column-wise to build a matrix of
                # duplicated sd vectors
                self.sd = np.tile(self.sd, (len(self.experimental_matrix), 1))

        # Second condition: the sd is a scalar and must be broadcast to a
        # matrix with same shape as the data
        elif isinstance(self.sd, int) or isinstance(self.sd, float):
            self.sd = np.full(self.experimental_matrix.shape, self.sd)
        else:
            raise RuntimeError("Unknown error")

    def _get_default_sds(self):
        """
        Build a default sd matrix. Default values:
            * Biomass: 0.2
            * Metabolites: 0.5
        :return: None
        """

        sds = [0.2]
        for name in range(len(self.name_vector) - 1):
            sds.append(0.5)
        self.sd = np.array(sds)
        self._build_sd_matrix()

    # TODO: add in model system
    def optimize(self):
        """
        Run optimization and build the simulated matrix
        from the optimized parameters
        """

        self.logger.info("\nRunning optimization...\n")
        self.optimize_results = PhysioFitter._run_optimization(
            self.params, self.simulate, self.experimental_matrix,
            self.time_vector, self.deg_vector,
            self.sd, self.bounds, "differential_evolution"
        )
        self.parameter_stats = {
            "optimal": self.optimize_results.x
        }
        self.logger.info(f"Optimization results: \n{self.optimize_results}\n")
        for i, param in zip(self.ids, self.optimize_results.x):
            self.logger.info(f"\n{i} = {param}\n")
        self.simulated_matrix = self.simulate(
            self.optimize_results.x,
            self.experimental_matrix,
            self.time_vector,
            self.deg_vector
        )
        nan_sim_mat = np.copy(self.simulated_matrix)
        nan_sim_mat[np.isnan(self.experimental_matrix)] = np.nan
        self.simulated_data = DataFrame(
            data=nan_sim_mat,
            index=self.time_vector,
            columns=self.name_vector
        )
        self.simulated_data.index.name = "Time"
        self.logger.info(f"Final Simulated Data: \n{self.simulated_data}\n")

    @staticmethod
    def _calculate_cost(
            params, func, exp_data_matrix, time_vector, deg, sd_matrix
    ):
        """
        Calculate the cost (residue) using the square of
        simulated-experimental over the SDs
        """

        simulated_matrix = func(params, exp_data_matrix, time_vector, deg)
        cost_val = np.square((simulated_matrix - exp_data_matrix) / sd_matrix)
        residuum = np.nansum(cost_val)
        return residuum

    @staticmethod
    def _run_optimization(
            params: list,
            func: Model,
            exp_data_matrix: np.ndarray,
            time_vector: np.ndarray,
            deg,
            sd_matrix: np.ndarray,
            bounds: dict,
            method: str
    ):
        """
        Run the optimization on input parameters using the cost function and
        Scipy minimize (L-BFGS-B method that is deterministic and uses the
        gradient method for optimizing)
        """

        if method == "differential_evolution":
            optimize_results = differential_evolution(
                PhysioFitter._calculate_cost, bounds=bounds,
                args=(func, exp_data_matrix, time_vector, deg, sd_matrix),
                polish=True, x0=params
            )
        elif method == "L-BFGS-B":
            optimize_results = minimize(
                PhysioFitter._calculate_cost, x0=params,
                args=(func, exp_data_matrix, time_vector, deg, sd_matrix),
                method="L-BFGS-B", bounds=bounds, options={'maxcor': 30}
            )
        else:
            raise ValueError(f"{method} is not implemented")

        return optimize_results

    def monte_carlo_analysis(self):
        """
        Run a monte carlo analysis to calculate optimization standard
        deviations on parameters and simulated data points
        """

        if not self.optimize_results:
            raise RuntimeError(
                "Running Monte Carlo simulation without having run the "
                "optimization is impossible as best fit results are needed to "
                "generate the initial simulated matrix"
            )

        self.logger.info(
            f"Running monte carlo analysis. Number of iterations: "
            f"{self.iterations}\n"
        )

        # Store the optimized results in variable that will be overridden on
        # every pass
        opt_res = self.optimize_results
        opt_params_list = []
        matrices = []

        for _ in range(self.iterations):
            new_matrix = self._apply_noise()

            # We optimise the parameters using the noisy matrix as input
            opt_res = PhysioFitter._run_optimization(
                opt_res.x, self.simulate, new_matrix, self.time_vector,
                self.deg_vector, self.sd, self.bounds, "L-BFGS-B"
            )

            # Store the new simulated matrix in list for later use
            matrices.append(
                self.simulate(
                    opt_res.x, new_matrix, self.time_vector, self.deg_vector
                )
            )

            # Store the new optimised parameters in list for later use
            opt_params_list.append(opt_res.x)

        # Build a 3D array from all the simulated matrices to get standard
        # deviation on each data point
        matrices = np.array(matrices)
        self.matrices_ci = {
            "lower_ci": np.percentile(matrices, 2.5, axis=0),
            "higher_ci": np.percentile(matrices, 97.5, axis=0)
        }

        # Compute the statistics on the list of parameters: means, sds,
        # medians and confidence interval
        self._compute_parameter_stats(opt_params_list)
        self.logger.info(f"Optimized parameters statistics:")
        for key, value in self.parameter_stats.items():
            self.logger.info(f"{key}: {value}")

        # Apply nan mask to be coherent with the experimental matrix
        nan_lower_ci = np.copy(self.matrices_ci['lower_ci'])
        nan_higher_ci = np.copy(self.matrices_ci['higher_ci'])
        nan_lower_ci[np.isnan(self.experimental_matrix)] = np.nan
        nan_higher_ci[np.isnan(self.experimental_matrix)] = np.nan

        self.logger.info(
            f"Simulated matrix lower confidence interval:\n{nan_lower_ci}\n"
        )
        self.logger.info(
            f"Simulated matrix higher confidence interval:\n{nan_higher_ci}\n"
        )
        return

    def _compute_parameter_stats(self, opt_params_list):
        """
        Compute statistics on the optimized parameters from the monte carlo
        analysis.

        :param opt_params_list: list of optimized parameter arrays generated
                                during the monte carlo analysis
        :return: parameter stats attribute containing means, sds, medians, low
                 and high CI
        """

        opt_params_means = np.mean(np.array(opt_params_list), 0)
        opt_params_sds = np.std(np.array(opt_params_list), 0)
        opt_params_meds = np.median(np.array(opt_params_list), 0)
        conf_ints = np.column_stack((
            np.percentile(opt_params_list, 2.5, 0),
            np.percentile(opt_params_list, 97.5, 0)
        ))

        self.parameter_stats.update({
            "mean": opt_params_means,
            "sd": opt_params_sds,
            "median": opt_params_meds,
            "CI_2.5": conf_ints[:, 0],
            "CI_97.5": conf_ints[:, 1]
        })

        # self.parameter_stats_df = DataFrame()

    def khi2_test(self):

        number_measurements = np.count_nonzero(
            ~np.isnan(self.experimental_matrix)
        )
        number_params = len(self.params)
        dof = number_measurements - number_params
        cost = self._calculate_cost(
            self.optimize_results.x, self.simulate, self.experimental_matrix,
            self.time_vector, self.deg_vector, self.sd
        )
        p_val = chi2.cdf(cost, dof)

        khi2_res = {
            "khi2_value": cost,
            "number_of_measurements": number_measurements,
            "number_of_params": number_params,
            "Degrees_of_freedom": dof,
            "p_val": p_val
        }
        self.khi2_res = DataFrame.from_dict(
            khi2_res, "index", columns=["Values"]
        )

        self.logger.info(f"khi2 test results:\n"
                         f"khi2 value: {cost}\n"
                         f"Number of measurements: {number_measurements}\n"
                         f"Number of parameters to fit: {number_params}\n"
                         f"Degrees of freedom: {dof}\n"
                         f"p-value = {p_val}\n")

        if p_val < 0.95:
            self.logger.info(
                f"At level of 95% confidence, the model fits the data good "
                f"enough with respect to the provided measurement SD. "
                f"Value: {p_val}"
            )

        else:
            self.logger.info(
                f"At level of 95% confidence, the model does not fit the data "
                f"good enough with respect to the provided measurement SD. "
                f"Value: {p_val}\n"
            )

    @staticmethod
    def _add_noise(vector, sd):
        """
        Add random noise to a given array using input standard deviations.

        :param vector: input array on which to apply noise
        :type vector: class: numpy.ndarray
        :param sd: standard deviation to apply to the input array
        :type sd: class: numpy.ndarray
        :return: noisy ndarray
        """

        output = np.random.default_rng().normal(
            loc=vector, scale=sd, size=vector.size
        )
        return output

    def _apply_noise(self):
        """
        Apply noise to the simulated matrix obtained using optimized
        parameters. SDs are obtained from the sd matrix
        """

        new_matrix = np.array([
            PhysioFitter._add_noise(self.simulated_matrix[idx, :], sd)
            for idx, sd in enumerate(self.sd)
        ])
        return new_matrix
