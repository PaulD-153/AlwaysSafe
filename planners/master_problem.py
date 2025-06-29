import cvxpy as cv
import numpy as np
from util.fairness_penalties import (
    variance_penalty, variance_penalty_gradient, variance_penalty_numpy
)


class MasterProblem:
    def __init__(self, agents, resource_capacity=1, langrangian_weight=1.0, fairness_type="variance", fairness_scope="timestep"):
        self.agents = agents
        self.horizon = agents[0].horizon
        self.num_agents = len(agents)
        self.resource_capacity = resource_capacity
        self.langrangian_weight = langrangian_weight
        self.fairness_type = fairness_type
        self.fairness_scope = fairness_scope  # "timestep" or "episode"

        self.decision_vars = []
        self.lp = None
        self.resource_constraints = []  # Save constraints to access duals

    def solve(self):
        constraints = []

        self.decision_vars = []
        
        # Create decision variables (nonnegative for stochastic plan selection)
        for agent in self.agents:
            agent_vars = []
            for _ in agent.get_columns():
                var = cv.Variable(nonneg=True)  # Stochastic: non-negative fractional
                agent_vars.append(var)
            self.decision_vars.append(agent_vars)

        # Each agent must select or probabilistic combination of columns
        for a in range(self.num_agents):
            constraints.append(cv.sum(self.decision_vars[a]) == 1)

        self.resource_constraints = []
        for t in range(self.horizon):
            expected_total_claims = 0
            for a, agent in enumerate(self.agents):
                columns = agent.get_columns()
                for c, column in enumerate(columns):
                    expected_total_claims += self.decision_vars[a][c] * column["claims"][t]
            constraint = (expected_total_claims <= self.resource_capacity)
            constraints.append(constraint)
            self.resource_constraints.append(constraint)

        # Objective: maximize expected total reward
        total_expected_reward = 0

        if self.langrangian_weight > 0:
            if self.fairness_scope == "timestep":
                # current version (per-timestep variance)
                for t in range(self.horizon):
                    expected_claims_t = []
                    for a in range(self.num_agents):
                        expr = 0
                        for c, column in enumerate(self.agents[a].get_columns()):
                            expr += self.decision_vars[a][c] * column["claims"][t]
                        expected_claims_t.append(expr)

                    if self.fairness_type == "variance":
                        fairness_penalty = variance_penalty(expected_claims_t)
                    total_expected_reward -= self.langrangian_weight * fairness_penalty

            elif self.fairness_scope == "cumulative":
                # new version (cumulative variance)
                expected_cumulative_claims = []
                for a in range(self.num_agents):
                    expr = 0
                    for c, column in enumerate(self.agents[a].get_columns()):
                        expr += self.decision_vars[a][c] * sum(column["claims"])  # sum across horizon
                    expected_cumulative_claims.append(expr)

                if self.fairness_type == "variance":
                    fairness_penalty = variance_penalty(expected_cumulative_claims)
                total_expected_reward -= self.langrangian_weight * fairness_penalty

            else:
                raise ValueError("Unknown fairness_scope option")

        for a, agent in enumerate(self.agents):
            for c, column in enumerate(agent.get_columns()):
                reward = np.sum(column["reward"])
                cost = np.sum(agent.fixed_cost_vector * column["claims"])  # new line
                net_value = reward - cost  # optionally add a cost_weight factor
                total_expected_reward += self.decision_vars[a][c] * net_value

        objective = cv.Maximize(total_expected_reward)
        self.lp = cv.Problem(objective, constraints)
        self.lp.solve(verbose=False)

        # print("Master LP Status:", self.lp.status)
        # print("Master LP Objective Value:", self.lp.value)

        # Compute fairness gradients after solving
        fairness_gradients_per_agent = self.compute_fairness_gradients()

        fairness_penalty_realized = self.compute_realized_fairness_penalty()
        fairness_impact = self.langrangian_weight * fairness_penalty_realized

        return self.lp.value, self.get_decision_distribution(), fairness_impact

    def compute_fairness_gradients(self):
        gradients_per_agent = []

        if self.fairness_scope == "timestep":
            # Existing per-timestep fairness gradients
            gradients = []
            for t in range(self.horizon):
                expected_claims_t = []
                for a in range(self.num_agents):
                    expr = 0
                    for c, column in enumerate(self.agents[a].get_columns()):
                        expr += self.decision_vars[a][c].value * column["claims"][t]
                    expected_claims_t.append(expr)

                if self.fairness_type == "variance":
                    grad_t = variance_penalty_gradient(expected_claims_t)
                else:
                    raise NotImplementedError("Only variance fairness implemented in gradient")

                gradients.append(grad_t)

            # Per-agent gradients across timesteps
            for a in range(self.num_agents):
                agent_grad = np.array([gradients[t][a] for t in range(self.horizon)])
                gradients_per_agent.append(agent_grad)

        elif self.fairness_scope == "cumulative":
            # Cumulative fairness gradients
            expected_cumulative_claims = []
            for a in range(self.num_agents):
                expr = 0
                for c, column in enumerate(self.agents[a].get_columns()):
                    expr += self.decision_vars[a][c].value * sum(column["claims"])
                expected_cumulative_claims.append(expr)

            if self.fairness_type == "variance":
                grad_cumulative = variance_penalty_gradient(expected_cumulative_claims)
            else:
                raise NotImplementedError("Only variance fairness implemented in gradient")

            # Distribute same gradient to each timestep
            for a in range(self.num_agents):
                agent_grad = np.array([grad_cumulative[a]] * self.horizon)
                gradients_per_agent.append(agent_grad)

        else:
            raise ValueError("Unknown fairness_scope option")

        return gradients_per_agent

    def get_dual_prices(self):
        return np.array([
            c.dual_value if c.dual_value is not None else 0.0
            for c in self.resource_constraints
        ])

    def get_decision_distribution(self):
        """
        Returns the full distribution over columns for each agent.
        """
        distributions = []
        for a_vars in self.decision_vars:
            weights = np.array([var.value if var.value is not None else 0.0 for var in a_vars])
            if np.sum(weights) == 0:
                weights = np.ones_like(weights) / len(weights)
            else:
                weights /= np.sum(weights)
            distributions.append(weights)
        return distributions
    
    def compute_realized_fairness_penalty(self):
        if self.fairness_scope == "timestep":
            penalty = 0.0
            for t in range(self.horizon):
                claims_t = []
                for a in range(self.num_agents):
                    val = 0.0
                    for c, column in enumerate(self.agents[a].get_columns()):
                        prob = self.decision_vars[a][c].value
                        val += prob * column["claims"][t]
                    claims_t.append(val)
            # Compute variance using actual numerical values
                penalty += variance_penalty_numpy(claims_t)
            return penalty

        elif self.fairness_scope == "cumulative":
            claims = []
            for a in range(self.num_agents):
                val = 0.0
                for c, column in enumerate(self.agents[a].get_columns()):
                    prob = self.decision_vars[a][c].value
                    val += prob * np.sum(column["claims"])
                claims.append(val)
            return variance_penalty_numpy(claims)

        else:
            raise ValueError("Unknown fairness_scope")
