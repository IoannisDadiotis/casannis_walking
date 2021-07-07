import casadi as cs 
import numpy as np
from matplotlib import pyplot as plt
import trj_interpolation as interpol

import costs
import constraints

class Gait:
    """
    Assumptions:
      1) mass concentrated at com
      2) zero angular momentum
      3) point contacts

    Dynamics:
      1) input is com jerk
      2) dynamics is a triple integrator of com jerk
      3) there must be contact forces that
        - realize the motion
        - fulfil contact constraints (i.e. unilateral constraint)
    """

    def __init__(self, mass, N, dt):
        """Gait class constructor

        Args:
            mass (float): robot mass
            N (int): horizon length
            dt (float): discretization step
        """

        self._N = N 
        self._dt = dt   # dt used for optimization knots
        self._mass = mass
        self._time = [(i * dt) for i in range(N)]

        gravity = np.array([0, 0, -9.81])

        # define dimensions
        sym_t = cs.SX
        self._dimc = dimc = 3
        self._dimx = dimx = 3 * dimc
        self._dimu = dimu = dimc
        self._dimf = dimf = dimc
        self._ncontacts = ncontacts = 4
        self._dimf_tot = dimf_tot = ncontacts * dimf

        # define cs variables
        delta_t = sym_t.sym('delta_t', 1)   # symbolic in order to pass values for optimization/interpolation
        c = sym_t.sym('c', dimc)
        dc = sym_t.sym('dc', dimc)
        ddc = sym_t.sym('ddc', dimc)
        x = cs.vertcat(c, dc, ddc) 
        u = sym_t.sym('u', dimc)

        # expression for the integrated state
        xf = cs.vertcat(
            c + dc*delta_t + 0.5*ddc*delta_t**2 + 1.0/6.0*u*delta_t**3,  # position
            dc + ddc*delta_t + 0.5*u*delta_t**2,  # velocity
            ddc + u*delta_t  # acceleration
        )

        # wrap the expression into a function
        self._integrator = cs.Function('integrator', [x, u, delta_t], [xf], ['x0', 'u', 'delta_t'], ['xf'])

        # construct the optimization problem (variables, cost, constraints, bounds)
        X = sym_t.sym('X', N * dimx)  # state is an SX for all knots
        U = sym_t.sym('U', N * dimu)  # for all knots
        F = sym_t.sym('F', N * (ncontacts * dimf))  # for all knots
        P = list()
        g = list()  # list of constraint expressions
        J = list()  # list of cost function expressions

        self._trj = {
            'x': X,
            'u': U,
            'F': F
        }

        # iterate over knots starting from k = 0
        for k in range(N):

            x_slice1 = k * dimx
            x_slice2 = (k + 1) * dimx
            u_slice1 = k * dimu
            u_slice2 = (k + 1) * dimu
            f_slice1 = k * dimf_tot
            f_slice2 = (k + 1) * dimf_tot

            # contact points
            p_k = sym_t.sym('p_' + str(k), ncontacts * dimc)
            P.append(p_k)

            # cost  function
            cost_function = 0.0
            cost_function += costs.penalize_horizontal_CoM_position(1e2, X[x_slice1:x_slice1 + 3],
                                                                    p_k)  # penalize CoM position
            cost_function += costs.penalize_vertical_CoM_position(1e3, X[x_slice1:x_slice1 + 3], p_k)
            cost_function += costs.penalize_xy_forces(1e-3, F[f_slice1:f_slice2])  # penalize xy forces
            cost_function += costs.penalize_quantity(1e-0,
                                                     U[u_slice1:u_slice2])  # penalize CoM jerk, that is the control
            J.append(cost_function)

            # newton - euler dynamic constraints
            newton_euler_constraint = constraints.newton_euler_constraint(
                X[x_slice1:x_slice2], mass, ncontacts, F[f_slice1:f_slice2], p_k
            )
            g.append(newton_euler_constraint['newton'])
            g.append(newton_euler_constraint['euler'])

            # state constraint (triple integrator)
            if k > 0:
                x_old = X[(k - 1) * dimx: x_slice1]  # save previous state
                u_old = U[(k - 1) * dimu: u_slice1]  # prev control
                x_curr = X[x_slice1:x_slice2]
                state_constraint = constraints.state_constraint(
                    self._integrator(x0=x_old, u=u_old, delta_t=dt)['xf'], x_curr)
                g.append(state_constraint)

        # construct the solver
        self._nlp = {
            'x': cs.vertcat(X, U, F),
            'f': sum(J),
            'g': cs.vertcat(*g),
            'p': cs.vertcat(*P)
            }

        # save dimensions
        self._nvars = self._nlp['x'].size1()
        self._nconstr = self._nlp['g'].size1()
        self._nparams = self._nlp['p'].size1()

        solver_options = {
            'ipopt.linear_solver': 'ma57'
        }

        self._solver = cs.nlpsol('solver', 'ipopt', self._nlp, solver_options)

    def solve(self, x0, contacts, swing_id, swing_tgt, swing_clearance, swing_t, min_f=50):
        """Solve the stepping problem

        Args:
            x0 ([type]): initial state (com position, velocity, acceleration)
            contacts ([type]): list of contact point positions
            swing_id ([type]): the indexes of the legs to be swinged from 0 to 3
            swing_tgt ([type]): the target footholds for the swing legs
            swing_clearance: clearance achieved from the highest point between initial and target position
            swing_t ([type]): list of lists with swing times in secs
            min_f: minimum threshold for forces in z direction
        """

        # lists for assigning bounds
        Xl = [0] * self._dimx * self._N  # state lower bounds (for all knots)
        Xu = [0] * self._dimx * self._N  # state upper bounds
        Ul = [0] * self._dimu * self._N  # control lower bounds
        Uu = [0] * self._dimu * self._N  # control upper bounds
        Fl = [0] * self._dimf_tot * self._N  # force lower bounds
        Fu = [0] * self._dimf_tot * self._N  # force upper bounds
        gl = list()  # constraint lower bounds
        gu = list()  # constraint upper bounds
        P = list()  # parameter values

        # time that maximum clearance occurs
        clearance_times = [0.5 * (x[0] + x[1]) for x in swing_t]

        # number of steps
        step_num = len(swing_id)

        # swing feet positions at maximum clearance
        clearance_swing_position = []

        print("Contacts are:", contacts)
        print("swing target:", swing_tgt)
        print("swing id:", swing_id)
        print("step number:", step_num)

        for i in range(step_num):
            if contacts[swing_id[i]][2] >= swing_tgt[i][2]:
                clearance_swing_position.append(contacts[swing_id[i]][0:2].tolist() +
                                                [contacts[swing_id[i]][2] + swing_clearance])
            else:
                clearance_swing_position.append(contacts[swing_id[i]][0:2].tolist() +
                                                [swing_tgt[i][2] + swing_clearance])

        # iterate over knots starting from k = 0
        for k in range(self._N):

            # slice indices for bounds at knot k
            x_slice1 = k * self._dimx
            x_slice2 = (k + 1) * self._dimx
            u_slice1 = k * self._dimu
            u_slice2 = (k + 1) * self._dimu
            f_slice1 = k * self._dimf_tot
            f_slice2 = (k + 1) * self._dimf_tot

            # state bounds
            state_bounds = constraints.bound_state_variables(x0, [np.full(9, -cs.inf), np.full(9, cs.inf)], k)
            Xu[x_slice1:x_slice2] = state_bounds['max']
            Xl[x_slice1:x_slice2] = state_bounds['min']

            # ctrl bounds
            u_max = np.full(self._dimu, cs.inf) # do not bound control
            u_min = -u_max

            Uu[u_slice1:u_slice2] = u_max
            Ul[u_slice1:u_slice2] = u_min

            # force bounds
            force_bounds = constraints.bound_force_variables(min_f, 1500, k, swing_t, swing_id, self._ncontacts,
                                                             self._dt, step_num)
            Fu[f_slice1:f_slice2] = force_bounds['max']
            Fl[f_slice1:f_slice2] = force_bounds['min']

            # contact positions
            contact_params = constraints.set_contact_parameters(
                contacts, swing_id, swing_tgt, clearance_times, clearance_swing_position, k, self._dt
            )
            P.append(contact_params)

            if k > 0:
                gl.append(np.zeros(self._dimx))
                gu.append(np.zeros(self._dimx))

            # constraint bounds (newton-euler eq.)
            gl.append(np.zeros(6)) 
            gu.append(np.zeros(6)) 

        # final constraints
        Xl[-6:] = [0.0 for i in range(6)]  # zero velocity and acceleration
        Xu[-6:] = [0.0 for i in range(6)]

        # initial guess
        v0 = np.zeros(self._nvars)

        # format bounds and params according to solver
        lbv = cs.vertcat(Xl, Ul, Fl)
        ubv = cs.vertcat(Xu, Uu, Fu)
        lbg = cs.vertcat(*gl)
        ubg = cs.vertcat(*gu)
        params = cs.vertcat(*P)

        # compute solution-call solver
        sol = self._solver(x0=v0, lbx=lbv, ubx=ubv, lbg=lbg, ubg=ubg, p=params)

        # plot state, forces, control input, quantities to be computed by evaluate function
        x_trj = cs.horzcat(self._trj['x'])  # pack states in a desired matrix
        f_trj = cs.horzcat(self._trj['F'])  # pack forces in a desired matrix
        u_trj = cs.horzcat(self._trj['u'])  # pack control inputs in a desired matrix

        # return values of the quantities *_trj
        return {
            'x': self.evaluate(sol['x'], x_trj),
            'F': self.evaluate(sol['x'], f_trj),
            'u': self.evaluate(sol['x'], u_trj)
        }
        
    def evaluate(self, solution, expr):
        """ Evaluate a given expression

        Args:
            solution: given solution
            expr: expression to be evaluated

        Returns:
            Numerical value of the given expression

        """

        # casadi function that symbolically maps the _nlp to the given expression
        expr_fun = cs.Function('expr_fun', [self._nlp['x']], [expr], ['v'], ['expr'])

        expr_value = expr_fun(v=solution)['expr'].toarray()

        # make it a flat list
        expr_value_flat = [i for sublist in expr_value for i in sublist]

        return expr_value_flat

    def interpolate(self, solution, sw_curr, sw_tgt, clearance, sw_t, resol):
        """ Interpolate the trajectories generated by the solution of the problem

        Args:
            solution: solution of the problem (numerical values) is a directory
                solution['x'] 9x30 --> optimized states
                solution['f'] 12x30 --> optimized forces
                solution['u'] 9x30 --> optimized control
            sw_curr: initial position of the feet to be swinged
            sw_tgt: target position of the feet to be swinged
            clearance: swing clearance
            sw_t: list of lists with swing times in secs e.g. [[a, b], [c, d]]
            resol: interpolation resolution (points per second)

        Returns: a dictionary with:
            time list for interpolation times (in sec)
            list of list with state trajectory points
            list of lists with forces' trajectory points
            list of lists with the swinging feet's trajectory points

        """

        # start and end times of optimization problem
        t_tot = [0.0, self._N * self._dt]

        # state
        state_trajectory = self.state_interpolation(solution=solution, resolution=resol)

        # forces
        forces_trajectory = self.forces_interpolation(solution=solution)

        # swing leg trajectory planning & interpolation
        sw_interpl = []
        for i in range(len(sw_curr)):
            # swing trajectories with one intemediate point
            sw_interpl.append(interpol.swing_trj_triangle(sw_curr[i], sw_tgt[i], clearance, sw_t[i], t_tot, resol))
            # spline optimization
            #sw_interpl.append(interpol.swing_trj_optimal_spline(sw_curr[i], sw_tgt[i], clearance, sw_t[i], t_tot, resol))

        return {
            't': self._t,
            'x': state_trajectory,
            'f': forces_trajectory,
            'sw': sw_interpl
        }

    def state_interpolation(self, solution, resolution):
        """
        Interpolate the state vector of the problem using the state equation
        :param solution: optimal solution
        :param resolution: resolution of the trajectory, points per secong
        :return: list of list with state trajectory points
        """

        delta_t = 1.0 / resolution  # dt for interpolation

        # intermediate points between two knots --> time interval * resolution
        self._n = int(self._dt * resolution)

        x_old = solution['x'][0:9]  # initial state
        x_all = []  # list to append all states

        for ii in range(self._N):  # loop for knots
            # control input to change in every knot
            u_old = solution['u'][self._dimu * ii:self._dimu * (ii + 1)]
            for j in range(self._n):  # loop for interpolation points
                x_all.append(x_old)  # storing state in the list 600x9
                x_next = self._integrator(x_old, u_old, delta_t)  # next state
                x_old = x_next  # refreshing the current state
            # initialize state and time lists to gather the data
        int_state = [[] for i in range(self._dimx)]  # primary dimension = number of state components
        self._t = [(ii * delta_t) for ii in range(self._N * self._n)]
        for i in range(self._dimx):  # loop for every component of the state vector
            for j in range(self._N * self._n):  # loop for every point of interpolation
                # append the value of x_i component on j point of interpolation
                # in the element i of the list int_state
                int_state[i].append(x_all[j][i])

        return int_state

    def forces_interpolation(self, solution):
        """
        Linear interpolation of the optimal forces
        :param solution: optimal solution
        :return: list of lists with the force trajectories
        """

        force_func = [[] for i in range(self._dimf_tot)]  # list to store the splines
        int_force = [[] for i in range(self._dimf_tot)]  # list to store lists of points

        for i in range(self._dimf_tot):  # loop for each component of the force vector

            # append the spline (by casadi) in the i element of the list force_func
            force_func[i].append(cs.interpolant('X_CONT', 'linear',
                                                [self._time],
                                                solution['F'][i::self._dimf_tot]))
            # store the interpolation points for each force component in the i element of the list int_force
            # primary dimension = number of force components
            int_force[i] = force_func[i][0](self._t)

        return int_force

    def print_trj(self, solution, results, resol, t_exec=[0, 0, 0, 0]):
        '''

        Args:
            solution: optimized decision variables
            t_exec: list of last trj points that were executed (because of early contact or other)
            results: results from interpolation
            resol: interpolation resol
            publish_freq: publish frequency - applies only when trajectories are interfaced with rostopics,
                else publish_freq = resol

        Returns: prints the nominal interpolated trajectories

        '''

        # Interpolated state plot
        state_labels = ['CoM Position', 'CoM Velocity', 'CoM Acceleration']
        plt.figure()
        for i, name in enumerate(state_labels):
            plt.subplot(3, 1, i + 1)
            for j in range(self._dimc):
                plt.plot(results['t'], results['x'][self._dimc * i + j], '-')
                #plt.plot([i * self._dt for i in range(self._N)], solution['x'][self._dimc * i + j], '.')
            plt.grid()
            plt.legend(['x', 'y', 'z'])
            plt.title(name)
        plt.xlabel('Time [s]')
        # plt.savefig('../plots/gait_state_trj.png')

        feet_labels = ['front left', 'front right', 'hind left', 'hind right']

        # Interpolated force plot
        plt.figure()
        for i, name in enumerate(feet_labels):
            plt.subplot(2, 2, i + 1)
            for k in range(3):
                plt.plot(results['t'], results['f'][3 * i + k], '-')
            plt.grid()
            plt.title(name)
            plt.legend([str(name) + '_x', str(name) + '_y', str(name) + '_z'])
        plt.xlabel('Time [s]')
        # plt.savefig('../plots/gait_forces.png')

        # plot swing trajectory
        # All points to be published
        N_total = int(self._N * self._dt * resol)  # total points --> total time * frequency
        s = np.linspace(0, self._dt * self._N, N_total)
        coord_labels = ['x', 'y', 'z']
        for j in range(len(results['sw'])):
            plt.figure()
            for i, name in enumerate(coord_labels):
                plt.subplot(3, 1, i + 1)
                plt.plot(s, results['sw'][j][name])  # nominal trj
                plt.plot(s[0:t_exec[j]], results['sw'][j][name][0:t_exec[j]])  # executed trj
                plt.grid()
                plt.legend(['nominal', 'real'])
                plt.title('Trajectory ' + name)
            plt.xlabel('Time [s]')
            # plt.savefig('../plots/gait_swing.png')

        # plot swing trajectory in two dimensions Z - X
        plt.figure()
        for j in range(len(results['sw'])):
            plt.subplot(2, 2, j + 1)
            plt.plot(results['sw'][j]['x'], results['sw'][j]['z'])  # nominal trj
            plt.plot(results['sw'][j]['x'][0:t_exec[j]], results['sw'][j]['z'][0:t_exec[j]])  # real trj
            plt.grid()
            plt.legend(['nominal', 'real'])
            plt.title('Trajectory Z- X')
            plt.xlabel('X [m]')
            plt.ylabel('Z [m]')
            # plt.savefig('../plots/gait_swing_zx.png')

        plt.show()


if __name__ == "__main__":

    w = Gait(mass=95, N=80, dt=0.1)

    # initial state
    #c0 = np.array([-0.00629, -0.03317, 0.01687])
    c0 = np.array([0.107729, 0.0000907, -0.02118])
    #c0 = np.array([-0.03, -0.04, 0.01687])
    dc0 = np.zeros(3)
    ddc0 = np.zeros(3)
    x_init = np.hstack([c0, dc0, ddc0])

    foot_contacts = [
        np.array([0.35, 0.35, -0.7187]),   # fl
        np.array([0.35, -0.35, -0.7187]),  # fr
        np.array([-0.35, 0.35, -0.7187]),    # hl
        np.array([-0.35, -0.35, -0.7187])   # hr
    ]

    # swing id from 0 to 3
    #sw_id = 2
    sw_id = [0, 1]

    #swing_target = np.array([-0.35, -0.35, -0.719])
    dx = 0.1
    dy = 0.0
    dz = 0.0
    swing_target = np.array([[foot_contacts[sw_id[0]][0] + dx, foot_contacts[sw_id[0]][1] + dy, foot_contacts[sw_id[0]][2] + dz],
                             [foot_contacts[sw_id[1]][0] + dx, foot_contacts[sw_id[1]][1] + dy, foot_contacts[sw_id[1]][2] + dz]])

    # swing_time
    #swing_time = [[1.0, 4.0], [5.0, 8.0]]
    swing_time = [[1.0, 2.5], [3.5, 5.0]]

    step_clear = 0.05

    # sol is the directory returned by solve class function contains state, forces, control values
    sol = w.solve(x0=x_init, contacts=foot_contacts, swing_id=sw_id, swing_tgt=swing_target,
                  swing_clearance=step_clear, swing_t=swing_time, min_f=100)
    # debug
    print("X0 is:", x_init)
    print("contacts is:", foot_contacts)
    print("swing id is:", sw_id)
    print("swing target is:", swing_target)
    print("swing time:", swing_time)
    # interpolate the values, pass values and interpolation resolution
    res = 300

    swing_currents = [foot_contacts[sw_id[0]], foot_contacts[sw_id[1]]]
    interpl = w.interpolate(sol, swing_currents, swing_target, step_clear, swing_time, res)

    # print the results
    w.print_trj(sol, interpl, res)


