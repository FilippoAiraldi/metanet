import casadi as cs

from .origins import MainstreamOrigin, OnRamp
from .links import LinkWithVms
from .networks import Network
from . import functional as F


class Simulation:
    def __init__(self, net: Network, T: float, rho_max: float, eta: float, tau: float,
                 kappa: float, delta: float, alpha: float = 0.0) -> None:
        '''
        Creates a simulation environment

        Parameters
        ----------
            net : metanet.Network
                Model of the network. Its topology cannot be changed after
                starting the simulation

            T : float
                Simulation time-step.

            eta, tau, kappa, delta : float
                Model parameters. TODO: better description.

            rho_max : float
                Maximum density of the network.

            alpha : float
                Adherence to Variable Message Sign speed limits. Represents a
                percentual increase over this limits (i.e., 0.1 increases the
                actual limit by 10%).
        '''
        # save parameters
        self.net = net
        self.T = T
        self.eta = eta
        self.tau = tau
        self.kappa = kappa
        self.delta = delta
        self.rho_max = rho_max
        self.alpha = alpha

        # some checks
        self.__check_normalized_turnrates()
        self.__check_main_origins_and_destinations()

    def __check_normalized_turnrates(self):
        # check turnrates are normalized
        sums_per_node = {}
        for (node, _), rate in self.net.turnrates.items():
            s = sums_per_node.get(node, 0)
            sums_per_node[node] = s + rate
        for node, s in sums_per_node.items():
            if s != 1.0:
                raise ValueError(f'Turn-rates at node {node.name} do not sum '
                                 f'to 1; got {s} instead.')

    def __check_main_origins_and_destinations(self):
        # all nodes that have a mainstream origin must have no entering links
        # for origin, nodedata in self.net.origins.items():
        for origin, nodedata in self.net.mainstream_origins:
            if len(nodedata.links_in) != 0:
                raise ValueError(
                    f'Node {nodedata.node.name} should have no entering links '
                    'since it is connected to the mainstream origin '
                    f'{origin.name}; got {len(nodedata.links_in)} entering '
                    'links instead.')

        # all nodes that have a destination must have no exiting links
        for destination, nodedata in self.net.destinations.items():
            if len(nodedata.links_out) != 0:
                raise ValueError(
                    f'Node {nodedata.node.name} should have no exiting links '
                    'since it is connected to the destination '
                    f'{destination.name}; got {len(nodedata.links_out)} '
                    'exiting links instead.')

    def _check_unique_names(self):
        names = set()
        for o in (list(self.net.nodes.keys())
                  + list(self.net.links.keys())
                  + list(self.net.origins.keys())
                  + list(self.net.destinations.keys())):
            name = o.name
            if name in names:
                raise ValueError(
                    f'{o.__class__.__name__} {name} has a non-unique name.')
            names.add(name)

    def set_init_cond(self, links_init=None, origins_init=None,
                      reset=False):
        '''
        Reset internal variables and sets the initial conditions for the
        model's simulation.

        Parameters
        ----------
            links_init : dict[metanet.Link, (array, array)], optional
                Dictionary initial conditions from link to (density, speed).

            origins_init : dict[metanet.Origin, float], optional
                Dictionary initial conditions from origin to queue.

            reset : bool, optional
                Resets all the internal quantities. Defaults to False.
        '''

        if links_init is not None:
            for link, (rho, v) in links_init.items():
                if reset:
                    link.reset()
                link.density[-1] = rho.reshape((link.nb_seg, 1))
                link.speed[-1] = v.reshape((link.nb_seg, 1))

        if origins_init is not None:
            for origin, w in origins_init.items():
                if reset:
                    origin.reset()
                origin.queue[-1] = w.reshape((1, 1))

    def step(self, k: int):
        '''
        Steps the model's simulation one timestep.

        Parameters
        ----------
            k : int
                Simulation timestep index.
        '''
        self.__step_origins(k)
        self.__step_links(k)

    def __step_origins(self, k: int):
        for origin, nodedata in self.net.origins.items():
            # first link after origin - assume only one link out
            link = nodedata.links_out[0].link

            # compute flow
            if isinstance(origin, MainstreamOrigin):
                origin.flow[k] = F.get_mainorigin_flow(
                    d=origin.demand[k],
                    w=origin.queue[k],
                    v_ctrl=(link.v_ctrl_at(k, 0)
                            if isinstance(link, LinkWithVms) else
                            cs.inf),
                    v_first=link.speed[k][0],
                    rho_crit=link.rho_crit,
                    v_free=link.v_free,
                    a=link.a,
                    lanes=link.lanes,
                    alpha=self.alpha,
                    T=self.T)
            elif isinstance(origin, OnRamp):
                origin.flow[k] = F.get_onramp_flow(
                    d=origin.demand[k],
                    w=origin.queue[k],
                    C=origin.capacity,
                    r=origin.rate[k],
                    rho_max=self.rho_max,
                    rho_first=link.density[k][0],
                    rho_crit=link.rho_crit,
                    T=self.T)
            else:
                raise ValueError(f'Unknown origin type {origin.__class__}.')

            # step queue
            origin.queue[k + 1] = F.step_origin_queue(
                w=origin.queue[k],
                d=origin.demand[k],
                q=origin.flow[k],
                T=self.T)

    def __step_links(self, k: int):
        for link, linkdata in self.net.links.items():
            node_up = linkdata.node_up
            node_down = linkdata.node_down

            # compute flow
            link.flow[k] = F.get_link_flow(
                rho=link.density[k],
                v=link.speed[k],
                lanes=link.lanes)

            # compute NODE & BOUNDARY conditions
            q_up = node_up.origin.flow[k] if node_up.origin is not None else 0
            if len(node_up.links_in) == 0:  # i.e., mainstream origin boundary
                v_up = link.speed[k][0]
            else:
                v_lasts, q_lasts = tuple(map(lambda o: cs.vertcat(*o), zip(
                    *[(L.link.speed[k][-1], L.link.flow[k][-1])
                      for L in node_up.links_in])))
                q_up += F.get_upstream_flow(
                    q_lasts, self.net.turnrates[node_up.node, link])
                v_up = F.get_upstream_speed(v_lasts, q_lasts)

            if len(node_down.links_out) == 0:  # i.e., destination boundary
                rho_down = cs.fmin(link.rho_crit, link.density[k][-1])
            else:
                rho_firsts = cs.vertcat(
                    *[L.link.density[k][0] for L in node_down.links_out])
                rho_down = F.get_downstream_density(rho_firsts)

            # put in vector form
            q_up = cs.vertcat(q_up, link.flow[k][:-1])
            v_up = cs.vertcat(v_up, link.speed[k][:-1])
            rho_down = cs.vertcat(link.density[k][1:], rho_down)

            # step density
            link.density[k + 1] = F.step_link_density(
                rho=link.density[k],
                q=link.flow[k],
                q_up=q_up,
                lanes=link.lanes,
                L=link.lengths,
                T=self.T)

            # compute equivalent speed
            V = F.Veq_ext(rho=link.density[k],
                          v_free=link.v_free,
                          a=link.a,
                          rho_crit=link.rho_crit,
                          v_ctrl=(link.v_ctrl_at(k)
                                  if isinstance(link, LinkWithVms) else
                                  cs.inf),
                          alpha=self.alpha)

            # step speed
            onramp = node_up.origin  # possible onramp connected to the link
            q_r = (onramp.flow[k]
                   if onramp is not None and isinstance(onramp, OnRamp) else
                   0)
            link.speed[k + 1] = F.step_link_speed(
                v=link.speed[k],
                v_up=v_up,
                rho=link.density[k],
                rho_down=rho_down,
                V=V,
                lanes=link.lanes,
                L=link.lengths,
                tau=self.tau,
                eta=self.eta,
                kappa=self.kappa,
                q_r=q_r,
                delta=self.delta,
                T=self.T)