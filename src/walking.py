import casadi as cs 
import numpy as np
from matplotlib import pyplot as plt
from scipy.stats import norm
from operator import add
import math

class Walking:
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
        """Walking class constructor

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
        X = list()
        U = list()
        F = list()
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

            # create k-th state
            x_k = sym_t.sym('x_' + str(k), dimx)
            X.append(x_k)

            # create k-th control
            u_k = sym_t.sym('u_' + str(k), dimu)
            U.append(u_k)

            # dynamics constraint
            if k > 0:
                x_old = X[-2]  # save previous state
                u_old = U[-2]  # prev control
                dyn_k = self._integrator(x0=x_old, u=u_old, delta_t=dt)['xf'] - x_k
                g.append(dyn_k)

            # forces
            f_k = sym_t.sym('f_' + str(k), ncontacts * dimf)
            F.append(f_k)

            # contact points
            p_k = sym_t.sym('p_' + str(k), ncontacts * dimc)
            P.append(p_k)

            # cost  function

            # horizontal distance of CoM from the mean of contact points
            h_horz = x_k[0:2] - 0.25 * (p_k[0:2] + p_k[3:5] + p_k[6:8] + p_k[9:11])  # xy

            # vertical distance between CoM and mean of feet
            h_vert = x_k[2] - 0.25 * (p_k[2] + p_k[5] + p_k[8] + p_k[11]) - 0.66

            j_k = 1e1 * cs.sumsqr(h_horz) + 1e3 * cs.sumsqr(h_vert) + 1e-2 * cs.sumsqr(u_k) + 1e-3 * cs.sumsqr(
                f_k[0::3]) + 1e-3 * cs.sumsqr(f_k[1::3])

            # debug trials
            # horizontal distance of CoM from each foot
            # distances = [(cs.sumsqr(x_k[0:2] - p_k[3*i:(3*i+2)]) - 0.495 ** 2) ** 2 for i in range(4)]   # xy
            # j_k = 1e1 * sum(distances) + 1e-2 * cs.sumsqr(u_k) + 1e-3 * cs.sumsqr(f_k[0::3]) + 1e-3 * cs.sumsqr(f_k[1::3])
            '''r = np.array([5e1, 5e1, 5e0, 1e0, 1e0, 1e5, 1e0, 1e0, 1e0])
            R = np.diag(r)
            j_k = cs.mtimes(cs.transpose(x_k), cs.mtimes(R, x_k)) + 1e1 * cs.sumsqr(u_k) #+ 1e1 * cs.sumsqr(f_k)'''
            # j_k = 0.5 * cs.sumsqr(u_k) + 1e-12 * cs.sumsqr(f_k) + 5e-0 * cs.sumsqr(x_k[0:2])

            J.append(j_k)

            # newton
            ddc_k = x_k[6:9]
            newton = mass*ddc_k - mass*gravity
            for i in range(ncontacts):
                f_i_k = f_k[3*i:3*(i+1)]  # force of i-th contact
                newton -= f_i_k

            g.append(newton)

            # euler
            c_k = x_k[0:3]
            euler = np.zeros(dimf)
            for i in range(ncontacts):
                f_i_k = f_k[3*i:3*(i+1)]  # force of i-th contact
                p_i_k = p_k[3*i:3*(i+1)]  # contact of i-th contact
                euler += cs.cross(p_i_k - c_k, f_i_k)

            g.append(euler)

        # construct the solver
        self._nlp = {
            'x': cs.vertcat(*X, *U, *F),
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

    def solve(self, x0, contacts, swing_id, swing_tgt, swing_t, min_f=50):
        """Solve the stepping problem

        Args:
            x0 ([type]): initial state (com position, velocity, acceleration)
            contacts ([type]): list of contact point positions
            swing_id ([type]): the index of the swing leg from 0 to 3
            swing_tgt ([type]): the target foothold for the swing leg
            swing_t ([type]): pair (t_lift, t_touch) in secs
            min_f: minimum threshold for forces in z direction
        """
        
        Xl = list()  # state lower bounds
        Xu = list()  # state upper bounds
        Ul = list()  # control lower bounds
        Uu = list()  # control upper bounds
        Fl = list()  # force lower bounds
        Fu = list()  # force upper bounds
        gl = list()  # constraint lower bounds
        gu = list()  # constraint upper bounds
        P = list()  # parameter values

        # iterate over knots starting from k = 0
        for k in range(self._N):

            # state bounds
            if k == 0:
                x_max = x0 
                x_min = x0

            else:
                #x_max = np.full(self._dimx, cs.inf) # do not bound state
                # constraining com z coordinate
                '''x_max = np.concatenate([[0.3], [0.12], [0.0], [0.1], [0.1], [0.1],  [0.1], [0.1], [0.1]])
                x_min = - np.concatenate([[0.13], [0.12], [0.15], [0.1], [0.1], [0.1], [0.1], [0.1], [0.1]])
                '''
                x_max = np.concatenate([[cs.inf], [cs.inf], [0.0], np.full(6, cs.inf)])
                x_min = -np.concatenate([[cs.inf], [cs.inf], [0.15], np.full(6, cs.inf)])
            # com not moving during swing motion
            '''elif k >= swing_t[0] / self._dt:
                x_max = np.concatenate([np.full(3, cs.inf), np.zeros(6)])
                x_min = -x_max'''

            Xu.append(x_max)
            Xl.append(x_min)

            # ctrl bounds
            u_max = np.full(self._dimu, cs.inf) # do not bound control
            u_min = -u_max 
            Uu.append(u_max)
            Ul.append(u_min)

            # force bounds
            f_max = np.full(self._dimf * self._ncontacts, cs.inf)
            f_min = np.array([-cs.inf, -cs.inf, min_f] * self._ncontacts)   # bound only the z component

            # swing phase
            is_swing = k >= swing_t[0]/self._dt and k <= swing_t[1]/self._dt
            if is_swing:
                # we are in swing phase
                f_max[3*swing_id:3*(swing_id+1)] = np.zeros(self._dimf) # overwrite forces for the swing leg
                f_min[3*swing_id:3*(swing_id+1)] = np.zeros(self._dimf)
            Fl.append(f_min)
            Fu.append(f_max)

            # contact positions
            p_k = np.hstack(contacts)  # start with initial contacts (4x3)
            if k >= swing_t[0]/self._dt:
                # after the swing, the swing foot is now at swing_tgt
                p_k[3*swing_id:3*(swing_id+1)] = swing_tgt

            P.append(p_k)
            
            # dynamics bounds
            if k > 0:
                gl.append(np.zeros(self._dimx))
                gu.append(np.zeros(self._dimx))

            # constraint bounds (newton-euler eq.)
            gl.append(np.zeros(6)) 
            gu.append(np.zeros(6)) 

        # final constraints
        Xl[-1][3:] = 0  # zero velocity and acceleration
        Xu[-1][3:] = 0

        # initial guess
        v0 = np.zeros(self._nvars)

        # format bounds and params according to solver
        lbv = cs.vertcat(*Xl, *Ul, *Fl)
        ubv = cs.vertcat(*Xu, *Uu, *Fu)
        lbg = cs.vertcat(*gl)
        ubg = cs.vertcat(*gu)
        params = cs.vertcat(*P)

        # compute solution-call solver
        sol = self._solver(x0=v0, lbx=lbv, ubx=ubv, lbg=lbg, ubg=ubg, p=params)

        # plot state, forces, control input, quantities to be computed by evaluate function
        x_trj = cs.horzcat(*self._trj['x']) # pack states in a desired matrix
        f_trj = cs.horzcat(*self._trj['F']) # pack forces in a desired matrix
        u_trj = cs.horzcat(*self._trj['u']) # pack control inputs in a desired matrix

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

        return expr_value

    def interpolate(self, solution, sw_curr, sw_tgt, clearance, sw_t, resol):
        """ Interpolate the trajectories generated by the solution of the problem

        Args:
            solution: solution of the problem (numerical values) is a directory
                solution['x'] 9x30 --> optimized states
                solution['f'] 12x30 --> optimized forces
                solution['u'] 9x30 --> optimized control
            sw_curr: current position of the foot to be swinged
            sw_tgt: target position of the foot to be swinged
            clearance: swing clearance
            sw_t: (start, stop) period of foot swinging in a global manner wrt to optimization problem
            resol: interpolation resolution (points per second)

        Returns: a dictionary with:
            time list for interpolation times (in sec)
            list of list with state trajectory points
            list of lists with forces' trajectory points
            list of lists with the swinging foot's trajectory points

        """

        # start and end times of optimization problem
        t_tot = [0.0, self._N * self._dt]

        delta_t = 1 / resol  # dt for interpolation

        # -------- state trajectory interpolation ------------

        # intermediate points between two knots --> time interval * resolution
        self._n = int(self._dt * resol)

        x_old = solution['x'][0:9, 0]  # initial state
        x_all = []  # list to append all states

        for ii in range(self._N):   # loop for knots

            # control input to change in every knot
            u_old = solution['u'][0:3, ii]

            for j in range(self._n):     # loop for interpolation points

                x_all.append(x_old)  # storing state in the list 600x9

                x_next = self._integrator(x_old, u_old, delta_t)    # next state
                x_old = x_next     # refreshing the current state

        # initialize state and time lists to gather the data
        int_state = [[] for i in range(self._dimx)]     # primary dimension = number of state components
        self._t = [(ii*delta_t) for ii in range(self._N * self._n)]

        for i in range(self._dimx): # loop for every component of the state vector
            for j in range(self._N * self._n):    # loop for every point of interpolation

                # append the value of x_i component on j point of interpolation
                # in the element i of the list int_state
                int_state[i].append(x_all[j][i])

        # ----------- force trajectory interpolation --------------

        force_func = [[] for i in range(len(solution['F']))]    # list to store the splines
        int_force = [[] for i in range(len(solution['F']))]     # list to store lists of points

        for i in range(len(solution['F'])): # loop for each component of the force vector

            # append the spline (by casadi) in the i element of the list force_func
            force_func[i].append(cs.interpolant('X_CONT', 'linear', [self._time], solution['F'][i]))

            # store the interpolation points for each force component in the i element of the list int_force
            # primary dimension = number of force components
            int_force[i] = force_func[i][0](self._t)

        # ----------- swing leg trajectory interpolation --------------

        # swing trajectory with intemediate point
        sw_interpl = self.swing_trj_triangle(sw_curr, sw_tgt, clearance, sw_t, t_tot, resol)

        # swing trajectory with gaussian function
        #sw_interpl = self.swing_trj_gaussian(sw_curr, sw_tgt, sw_t, t_tot, resol)

        return {
            't': self._t,
            'x': int_state,
            'f': int_force,
            'sw': sw_interpl
        }

    def swing_trj_gaussian(self, sw_curr, sw_tgt, sw_t, total_t, resol):
        '''
        Interpolates current and target foot position with a 5th order
        polynomial and superimposes a gaussian bell function to achieve clearance
        Args:
            sw_curr: current swing foot position
            sw_tgt: target swing foot position
            sw_t: swing phase time interval
            total_t: total time of optim. problem
            resol: interpolation resolution

        Returns:
            interpolated swing trajectory
        '''

        # current and target points
        sw_points = [sw_curr] + [sw_tgt]

        # list of the two points of swing phase for each coordinate
        sw_x = [sw_points[0][0], sw_points[1][0]]
        sw_y = [sw_points[0][1], sw_points[1][1]]
        sw_z = [sw_points[0][2], sw_points[1][2]]

        # conditions, initial point of swing phase
        init_x = [sw_x[0], 0, 0]
        init_y = [sw_y[0], 0, 0]
        init_z = [sw_z[0], 0, 0]

        # conditions, final point of swing phase
        fin_x = [sw_x[1], 0, 0]
        fin_y = [sw_y[1], 0, 0]
        fin_z = [sw_z[1], 0, 0]

        # save polynomial coefficients in one list for each coordinate
        sw_cx = self.splines(sw_t, init_x, fin_x)
        sw_cy = self.splines(sw_t, init_y, fin_y)
        sw_cz = self.splines(sw_t, init_z, fin_z)

        # convert to polynomial functions
        sw_px = np.polynomial.polynomial.Polynomial(sw_cx)
        sw_py = np.polynomial.polynomial.Polynomial(sw_cy)
        sw_pz = np.polynomial.polynomial.Polynomial(sw_cz)

        # construct list of interpolated points according to specified resolution
        sw_dt = sw_t[1] - sw_t[0]
        sw_interpl_t = np.linspace(sw_t[0], sw_t[1], int(resol * sw_dt))
        sw_interpl_x = sw_px(sw_interpl_t)
        sw_interpl_y = sw_py(sw_interpl_t)
        sw_interpl_z = sw_pz(sw_interpl_t)

        # number of interpolation points in non swing phases
        sw_n1 = int(resol * (sw_t[0] - total_t[0]))
        sw_n2 = int(resol * (total_t[1] - sw_t[1]))

        # append points for non swing phases
        sw_interpl_x = [sw_curr[0]] * sw_n1 + [sw_interpl_x[i] for i in range(len(sw_interpl_x))] + [sw_tgt[0]] * sw_n2
        sw_interpl_y = [sw_curr[1]] * sw_n1 + [sw_interpl_y[i] for i in range(len(sw_interpl_y))] + [sw_tgt[1]] * sw_n2
        sw_interpl_z = [sw_curr[2]] * sw_n1 + [sw_interpl_z[i] for i in range(len(sw_interpl_z))] + [sw_tgt[2]] * sw_n2

        # define bell function to adjust trajectory of z coordinate
        mean = 0.48 * (sw_t[0] + sw_t[1])  # center of normal distribution
        std = 0.14 * (sw_t[1] - sw_t[0])  # standard deviation
        sw_bellz = norm(loc=mean, scale=std)

        # weight of the bell function
        bell_w = 0.16   #0.08

        # list of points generated by the bell
        bell_trj = [bell_w * sw_bellz.pdf(self._t[i]) for i in range(self._N * self._n)]

        # sum bell with z trajectory
        sw_interpl_z = list(map(add, bell_trj, sw_interpl_z))

        # include all coordinates in a single list
        sw_interpol = [sw_interpl_x, sw_interpl_y, sw_interpl_z]

        return sw_interpol

    def swing_trj_triangle(self, sw_curr, sw_tgt, clear, sw_t, total_t, resol):
        '''
        Interpolates current, target foot position and a intermediate point with 5th order
        polynomials.
        Args:
            sw_curr: current foot position
            sw_tgt: target foot position
            clear: clearance (max height)
            sw_t: time interval of swing phase
            total_t: total time of optimization problem
            resol: interpolation resolution (points per sec)
            clear_loc: location of the achieved maximum clearance as a fraction of the dx an dy to be covered

        Returns:
            interpolated swing trajectory
        '''

        # intermediate point, clearance counted from the highest point
        if sw_curr[2] >= sw_tgt[2]:
            max_point = np.array([0.5 * (sw_curr[0] + sw_tgt[0]), 0.5 * (sw_curr[1] + sw_tgt[1]), sw_curr[2] + clear])
        else:
            max_point = np.array([0.5 * (sw_curr[0] + sw_tgt[0]), 0.5 * (sw_curr[1] + sw_tgt[1]), sw_tgt[2] + clear])

        # list of first and last point of swing phase
        sw_points = [sw_curr] + [max_point] + [sw_tgt]

        # list of the two points of swing phase for each coordinate
        sw_x = [sw_points[0][0], sw_points[1][0], sw_points[2][0]]
        sw_y = [sw_points[0][1], sw_points[1][1], sw_points[2][1]]
        sw_z = [sw_points[0][2], sw_points[1][2], sw_points[2][2]]

        # velocities x and y for the intermediate point
        vel_x = 3 * (sw_x[2] - sw_x[0]) / (sw_t[1] - sw_t[0])
        vel_y = 3 * (sw_y[2] - sw_y[0]) / (sw_t[1] - sw_t[0])
        print("Velocity x swing is :", vel_x)

        # conditions, initial point of swing phase
        cond1_x = [sw_x[0], 0, 0]
        cond1_y = [sw_y[0], 0, 0]
        cond1_z = [sw_z[0], 0, 0]

        # conditions, second point of swing phase
        cond2_x = [sw_x[1], vel_x, 0]
        cond2_y = [sw_y[1], vel_y, 0]
        cond2_z = [sw_z[1], 0, -0.2]

        # conditions, third point of swing phase
        cond3_x = [sw_x[2], 0, 0]
        cond3_y = [sw_y[2], 0, 0]
        cond3_z = [sw_z[2], 0, 0]

        # divide time in two
        sw_t1 = [sw_t[0], 0.5 * (sw_t[0] + sw_t[1])]
        sw_t2 = [0.5 * (sw_t[0] + sw_t[1]), sw_t[1]]

        # save polynomial coefficients in one list for each coordinate
        sw_cx1 = self.splines(sw_t1, cond1_x, cond2_x)   # spline 1
        sw_cy1 = self.splines(sw_t1, cond1_y, cond2_y)
        sw_cz1 = self.splines(sw_t1, cond1_z, cond2_z)

        sw_cx2 = self.splines(sw_t2, cond2_x, cond3_x)   # spline 2
        sw_cy2 = self.splines(sw_t2, cond2_y, cond3_y)
        sw_cz2 = self.splines(sw_t2, cond2_z, cond3_z)

        # convert to polynomial functions
        sw_px1 = np.polynomial.polynomial.Polynomial(sw_cx1)    # spline 1
        sw_py1 = np.polynomial.polynomial.Polynomial(sw_cy1)
        sw_pz1 = np.polynomial.polynomial.Polynomial(sw_cz1)

        sw_px2 = np.polynomial.polynomial.Polynomial(sw_cx2)    # spline 2
        sw_py2 = np.polynomial.polynomial.Polynomial(sw_cy2)
        sw_pz2 = np.polynomial.polynomial.Polynomial(sw_cz2)

        # construct list of interpolated points according to specified resolution
        sw_dt = sw_t[1] - sw_t[0]
        sw_interpl_t1 = np.linspace(sw_t1[0], sw_t1[1], int(0.5 * resol * sw_dt))
        sw_interpl_t2 = np.linspace(sw_t2[0], sw_t2[1], int(0.5 * resol * sw_dt))

        sw_interpl_x = np.concatenate((sw_px1(sw_interpl_t1), sw_px2(sw_interpl_t2)))
        sw_interpl_y = np.concatenate((sw_py1(sw_interpl_t1), sw_py2(sw_interpl_t2)))
        sw_interpl_z = np.concatenate((sw_pz1(sw_interpl_t1), sw_pz2(sw_interpl_t2)))

        # number of interpolation points in non swing phases
        sw_n1 = int(resol * (sw_t[0] - total_t[0]))
        sw_n2 = int(resol * (total_t[1] - sw_t[1]))

        # append points for non swing phases
        sw_interpl_x = [sw_curr[0]] * sw_n1 + [sw_interpl_x[i] for i in range(len(sw_interpl_x))] + [sw_tgt[0]] * sw_n2
        sw_interpl_y = [sw_curr[1]] * sw_n1 + [sw_interpl_y[i] for i in range(len(sw_interpl_y))] + [sw_tgt[1]] * sw_n2
        sw_interpl_z = [sw_curr[2]] * sw_n1 + [sw_interpl_z[i] for i in range(len(sw_interpl_z))] + [sw_tgt[2]] * sw_n2

        # compute arc length of swing trj
        sw_interpl_s = 0.0
        for i in range(len(sw_interpl_x) - 1):
            sw_interpl_ds = math.sqrt(
                (sw_interpl_x[i + 1] - sw_interpl_x[i]) ** 2 + (sw_interpl_y[i + 1] - sw_interpl_y[i]) ** 2 + (
                            sw_interpl_z[i + 1] - sw_interpl_z[i]) ** 2)
            sw_interpl_s += sw_interpl_ds

        return {
            'x': sw_interpl_x,
            'y': sw_interpl_y,
            'z': sw_interpl_z,
            's': sw_interpl_s
        }

    def splines(self, dt, init_cond, fin_cond):
        """
        This function computes the polynomial of 5th order between two points with 6 given conditions
        Args:
            dt: time interval that the polynomial applies
            init_cond: list with initial position, velocity and acceleration conditions
            fin_cond: list with final position, velocity and acceleration conditions

        Returns:
            a list with the coefficient of the polynomial of the spline
        """

        # symbolic polynomial coefficients
        sym_t = cs.SX
        a0 = sym_t.sym('a0', 1)
        a1 = sym_t.sym('a1', 1)
        a2 = sym_t.sym('a2', 1)
        a3 = sym_t.sym('a3', 1)
        a4 = sym_t.sym('a4', 1)
        a5 = sym_t.sym('a5', 1)

        # time
        t = sym_t.sym('t', 1)

        # initial and final conditions
        p0 = init_cond[0]
        v0 = init_cond[1]
        ac0 = init_cond[2]
        p1 = fin_cond[0]
        v1 = fin_cond[1]
        ac1 = fin_cond[2]
        #print('Initial and final conditions are:', p0, v0, ac0, p1, v1, ac1)

        # the 5th order polynomial expression
        spline = a0 + a1 * t + a2 * t ** 2 + a3 * t ** 3 + a4 * t ** 4 + a5 * t ** 5

        # wrap the polynomial expression in a function
        p = cs.Function('p', [t, a0, a1, a2, a3, a4, a5], [spline], ['t', 'a0', 'a1', 'a2', 'a3', 'a4', 'a5'],
                        ['spline'])

        # symbolic velocity - 1st derivative
        first_der = cs.jacobian(spline, t)
        dp = cs.Function('dp', [t, a1, a2, a3, a4, a5], [first_der], ['t', 'a1', 'a2', 'a3', 'a4', 'a5'], ['first_der'])

        # symbolic acceleration - 2nd derivative
        sec_der = cs.jacobian(first_der, t)
        ddp = cs.Function('ddp', [t, a2, a3, a4, a5], [sec_der], ['t', 'a2', 'a3', 'a4', 'a5'], ['sec_der'])

        # construct the system of equations Ax=B, with x the list of coefficients to be computed
        A = np.array([[p(dt[0], 1, 0, 0, 0, 0, 0), p(dt[0], 0, 1, 0, 0, 0, 0), p(dt[0], 0, 0, 1, 0, 0, 0),
                       p(dt[0], 0, 0, 0, 1, 0, 0), p(dt[0], 0, 0, 0, 0, 1, 0), p(dt[0], 0, 0, 0, 0, 0, 1)],\
                      [0, dp(dt[0], 1, 0, 0, 0, 0), dp(dt[0], 0, 1, 0, 0, 0), dp(dt[0], 0, 0, 1, 0, 0),
                       dp(dt[0], 0, 0, 0, 1, 0), dp(dt[0], 0, 0, 0, 0, 1)],\
                      [0, 0, ddp(dt[0], 1, 0, 0, 0), ddp(dt[0], 0, 1, 0, 0), ddp(dt[0], 0, 0, 1, 0),
                       ddp(dt[0], 0, 0, 0, 1)],\
                      [p(dt[1], 1, 0, 0, 0, 0, 0), p(dt[1], 0, 1, 0, 0, 0, 0), p(dt[1], 0, 0, 1, 0, 0, 0),
                       p(dt[1], 0, 0, 0, 1, 0, 0), p(dt[1], 0, 0, 0, 0, 1, 0), p(dt[1], 0, 0, 0, 0, 0, 1)],\
                      [0, dp(dt[1], 1, 0, 0, 0, 0), dp(dt[1], 0, 1, 0, 0, 0), dp(dt[1], 0, 0, 1, 0, 0),
                       dp(dt[1], 0, 0, 0, 1, 0), dp(dt[1], 0, 0, 0, 0, 1)],\
                      [0, 0, ddp(dt[1], 1, 0, 0, 0), ddp(dt[1], 0, 1, 0, 0), ddp(dt[1], 0, 0, 1, 0),
                       ddp(dt[1], 0, 0, 0, 1)]])

        B = np.array([p0, v0, ac0, p1, v1, ac1])

        # coefficients
        coeffs = np.linalg.inv(A).dot(B)

        return coeffs

    def print_trj(self, results, resol, publish_freq, t_exec=0):
        '''

        Args:
            t_exec: time that trj execution stopped (because of early contact or other)
            results: results from interpolation
            resol: interpolation resol
            publish_freq: publish frequency - applies only when trajectories are published through rostopics,
                else publish_freq = resol

        Returns: prints the nominal interpolated trajectories

        '''

        # scale time for the case of publish through rostopics
        time_scale = (resol/publish_freq)

        # Interpolated state plot
        state_labels = ['CoM Position', 'CoM Velocity', 'CoM Acceleration']
        plt.figure()
        for i, name in enumerate(state_labels):
            plt.subplot(3, 1, i + 1)
            for j in range(self._dimc):
                plt.plot([i * time_scale for i in results['t']], results['x'][self._dimc * i + j], '-')
            plt.grid()
            plt.legend(['x', 'y', 'z'])
            plt.title(name)
        plt.xlabel('Time [s]')
        #plt.savefig('../plots/step_state_trj.png')

        feet_labels = ['front left', 'front right', 'hind left', 'hind right']

        # Interpolated force plot
        plt.figure()
        for i, name in enumerate(feet_labels):
            plt.subplot(2, 2, i + 1)
            for k in range(3):
                plt.plot([i * time_scale for i in results['t']], results['f'][3 * i + k], '-')
            plt.grid()
            plt.title(name)
            plt.legend([str(name) + '_x', str(name) + '_y', str(name) + '_z'])
        plt.xlabel('Time [s]')
        #plt.savefig('../plots/step_forces.png')

        # plot swing trajectory
        # All points to be published
        N_total = int(self._N * self._dt * resol)  # total points --> total time * frequency
        s = np.linspace(0, self._dt * self._N, N_total)
        coord_labels = ['x', 'y', 'z']
        plt.figure()
        for i, name in enumerate(coord_labels):
            plt.subplot(3, 1, i + 1)
            plt.plot([i * time_scale for i in s], results['sw'][name])   # nominal trj
            plt.plot([i * time_scale for i in s[0:t_exec]], results['sw'][name][0:t_exec])   # executed trj
            plt.grid()
            plt.legend(['nominal', 'real'])
            plt.title('Trajectory ' + name)
        plt.xlabel('Time [s]')
        #plt.savefig('../plots/step_swing.png')

        # plot swing trajectory in two dimensions Z- X
        plt.figure()
        plt.plot(results['sw']['x'], results['sw']['z'])    # nominal trj
        plt.plot(results['sw']['x'][0:t_exec], results['sw']['z'][0:t_exec])    # real trj
        plt.grid()
        plt.legend(['nominal', 'real'])
        plt.title('Trajectory Z- X')
        plt.xlabel('X [m]')
        plt.ylabel('Z [m]')
        #plt.savefig('../plots/step_swing_zx.png')
        plt.show()


if __name__ == "__main__":

    w = Walking(mass=95, N=50, dt=0.1)

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
    sw_id = 0

    #swing_target = np.array([-0.35, -0.35, -0.719])
    dx = 0.1
    dy = 0.0
    dz = -0.05
    swing_target = np.array([foot_contacts[sw_id][0] + dx, foot_contacts[sw_id][1] + dy, foot_contacts[sw_id][2] + dz])

    #swing_time = (1.5, 3.0)
    swing_time = (1.0, 4.0)

    # sol is the directory returned by solve class function contains state, forces, control values
    sol = w.solve(x0=x_init, contacts=foot_contacts, swing_id=sw_id, swing_tgt=swing_target, swing_t=swing_time, min_f=100)
    # debug
    print("X0 is:", x_init)
    print("contacts is:", foot_contacts)
    print("swing id is:", sw_id)
    print("swing target is:", swing_target)
    print("swing time:", swing_time)
    # interpolate the values, pass values and interpolation resolution
    res = 300
    step_clear = 0.1
    interpl = w.interpolate(sol, foot_contacts[sw_id], swing_target, step_clear, swing_time, res)

    publish_hz = res
    # print the results
    w.print_trj(interpl, res, publish_hz)


