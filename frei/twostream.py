import astropy.units as u
import numpy as np
from astropy.constants import k_B, m_p, h, c, sigma_sb
from tqdm.auto import trange
from jax import numpy as jnp
from jax import lax, jit, checkpoint

from jax.scipy.ndimage import map_coordinates

from functools import partial

from .opacity import kappa

__all__ = [
    'propagate_fluxes',
    'emit'
]

flux_unit = u.erg / u.s / u.cm ** 3


def bolometric_flux(flux, lam):
    """
    Compute bolometric flux from wavelength-dependent flux
    """
    return jnp.trapz(flux, lam)


def delta_t_i(p_1, p_2, T_1, T_2, delta_F_i_dz, g, m_bar=2.4 * m_p, n_dof=5):
    """
    Timestep in iteration for radiative equilibrium.

    Follows Equations 27-28 of Malik et al. (2017).
    """
    dz = delta_z_i(T_1, p_1, p_2, g, m_bar)
    # Malik 2017 Eqn 28
    
    if (delta_F_i_dz * dz).value != 0:
        f_i_pre = 1e5 / (abs(delta_F_i_dz * dz) / (u.erg/u.cm**2/u.s))**0.9
    else: 
        f_i_pre = 1
    # Malik 2017 Eqn 27
    dt_radiative = c_p(m_bar=m_bar, n_dof=n_dof) * p_1 / sigma_sb / g / T_1 ** 3
    
    d_gamma = delta_gamma(T_1, T_2, p_1, p_2, g, m_bar=m_bar, n_dof=n_dof)
    if d_gamma > 0 * u.K / u.km:
        dt_convective = (T_1 / g / d_gamma) ** 0.5
        return f_i_pre * min(dt_radiative, dt_convective)
    return f_i_pre * dt_radiative


@jit
def E(omega_0, g_0):
    """
    Improved two-stream equation correction term.

    From Deitrick et al. (2020) Equation 19.

    Parameters
    ----------
    omega_0 : float or ~numpy.ndarray
        Single-scattering albedo
    g_0 : float or ~numpy.ndarray
        Scattering asymmetry factor

    Returns
    -------
    corr : ~numpy.ndarray
        Correction term, E(omega_0, g_0).
    """
    # Deitrick (2020) Eqn 19
    return jnp.where(
        omega_0 > 0.1,
        1.225 - 0.1582 * g_0 - 0.1777 * omega_0 - 0.07465 *
        g_0 ** 2 + 0.2351 * omega_0 * g_0 - 0.05582 * omega_0 ** 2,
        1
    )


@jit
def true_fun(x):
    return 1.0 / x

@jit
def false_fun(x):
    return 0.

@jit
def vectorized_cond(x):
    """https://github.com/google/jax/issues/1052"""
    # true_fun and false_fun must act elementwise (i.e. be vectorized)
    true_op = jnp.where(x > 0., x, 0.5)
    false_op = jnp.where(x > 0., 0.0, x)
    return jnp.where(
        x > 0., 
        true_fun(true_op), 
        false_fun(false_op)
    )

@jit
def BB(temperature, wavenumber):
    h = 6.62607015e-34  # J s
    c = 299792458.0e6  # um/s
    k_B = 1.380649e-23  # J/K
    one_over_denom = vectorized_cond(
        jnp.expm1(h * c / k_B * wavenumber * 
                  vectorized_cond(temperature))
    )
    return (1e18 * # convert microns to meters
        2 * h * c ** 2 * wavenumber ** 3 * one_over_denom
    )


@jit
def propagate_fluxes(
        lam, F_1_up, F_2_down, T_1, T_2, delta_tau, omega_0=0, g_0=0, eps=0.5
):
    """
    Compute fluxes up and down using the improved two-stream equations.

    The transmission function is taken from Deitrick et al. (2020) Equation B2.

    The two stream equations are taken from Malik et al. (2017)
    (see Equation 15), with corrections from Dietrick et al. (2022)
    (see Appendix B).

    Parameters
    ----------
    lam : ~astropy.units.Quantity
        Wavelength grid
    F_1_up : ~astropy.units.Quantity
        Flux up into layer 1
    F_2_down : ~astropy.units.Quantity
        Flux down into layer 2
    T_1 : ~astropy.units.Quantity
        Temperature in layer 1
    T_2 : ~astropy.units.Quantity
        Temperature in layer 2
    delta_tau : ~numpy.ndarray
        Change in optical depth
    omega_0 : ~numpy.ndarray or float
        Single scattering albedo
    g_0 : ~numpy.ndarray or float
        Scattering asymmetry factor
    eps : float
        First Eddington coefficient (Heng et al. 2014)

    Returns
    -------
    F_2_up, F_1_down : ~astropy.units.Quantity
        Fluxes outgoing to layer 2, and incoming to layer 1
    """
    omega_0 = omega_0
    delta_tau = delta_tau
    
    # Deitrick 2020 Equation B2
    T = jnp.exp(-2 * (E(omega_0, g_0) * (E(omega_0, g_0) - omega_0) *
                     (1 - omega_0 * g_0)) ** 0.5 * delta_tau)

    # Malik 2017 Equation 13
    zeta_plus = 0.5 * (1 + ((E(omega_0, g_0) - omega_0) / E(omega_0, g_0) /
                            (1 - omega_0 * g_0)) ** 0.5)
    zeta_minus = 0.5 * (1 - ((E(omega_0, g_0) - omega_0) / E(omega_0, g_0) /
                             (1 - omega_0 * g_0)) ** 0.5)

    # Malik 2017 Equation 12
    chi = zeta_minus ** 2 * T ** 2 - zeta_plus ** 2
    xi = zeta_plus * zeta_minus * (1 - T ** 2)
    psi = (zeta_minus ** 2 - zeta_plus ** 2) * T
    pi = jnp.pi * (1 - omega_0) / (E(omega_0, g_0) - omega_0)

    wavenumber = 1.0 / lam
    
    B1 = BB(T_1, wavenumber)
    B2 = BB(T_2, wavenumber)

    x = jnp.where(delta_tau == 0.0, 1.0, delta_tau)
    
    # Malik 2017 Equation 5
    Bprime = jnp.where(
        delta_tau == 0,
        0,
        (B1 - B2) / x,
    )

    # Deitrick 2022 Eqn B4
    F_2_up = (
        1 / chi * (
            psi * F_1_up - xi * F_2_down +
            pi * (B2 * (chi + xi) - psi * B1 +
            Bprime / (2 * E(omega_0, g_0) * (1 - omega_0 * g_0)) *
            (chi - psi - xi))
        )
    )
    F_1_down = (
        1 / chi * (
            psi * F_2_down - xi * F_1_up +
            pi * (B1 * (chi + xi) - psi * B2 +
            Bprime / (2 * E(omega_0, g_0) * (1 - omega_0 * g_0)) *
            (xi + psi - chi))
        )
    )
    return F_2_up, F_1_down


def delta_z_i(temperature_i, pressure_i, pressure_ip1, g, m_bar=2.4 * m_p):
    """
    Change in height in the atmosphere from bottom to top of a layer.

    Malik et al. (2017) Equation 18
    """
    return ((k_B * temperature_i) / (m_bar * g) *
            jnp.log(pressure_i / pressure_ip1))


def div_bol_net_flux(
    F_ip1_u, F_ip1_d, F_i_u, F_i_d, temperature_i, temperature_ip1, pressure_i, pressure_ip1,
    g, m_bar=2.4 * m_p, n_dof=5, alpha=1
):
    """
    Divergence of the bolometric net flux.

    Defined in Malik et al. (2017) Equation 23.
    """
    delta_F_rad = (F_ip1_u - F_ip1_d) - (F_i_u - F_i_d)
    
    delta_F_conv = convective_flux(temperature_i, temperature_ip1, 
                                   pressure_i, pressure_ip1, g, 
                                   m_bar=m_bar, n_dof=n_dof, alpha=alpha)
    dz = delta_z_i(temperature_i, pressure_i, pressure_ip1, g, m_bar)
    return (delta_F_rad + delta_F_conv) / dz, dz


def delta_temperature(
        div, p_1, p_2, T_1, delta_t_i, g, m_bar=2.4 * m_p, n_dof=5
):
    """
    Change in temperature in each layer after timestep for radiative equilibrium

    Defined in Malik et al. (2017) Equation 24
    """
    return (1 / rho_p(p_1, p_2, T_1, g, m_bar) / 
            c_p(m_bar, n_dof) * div * delta_t_i)


def c_p(m_bar=2.4 * m_p, n_dof=5):
    """
    Heat capacity, Malik et al. (2017) Equation 25
    """
    return (2 + n_dof) / (2 * m_bar) * k_B

@jit
def delta_tau_i(kappa_i, p_1, p_2, g):
    """
    Contribution to optical depth from layer i, Malik et al. (2017) Equation 19
    """
    return (p_1 - p_2) / g * kappa_i


def rho_p(p_1, p_2, T_1, g, m_bar=2.4 * m_p):
    """
    Local density.
    """
    return ((p_1 - p_2) / g) / delta_z_i(T_1, p_1, p_2, g, m_bar)


def gamma(temperature_i, temperature_ip1, pressure_i, pressure_ip1, g, m_bar=2.4 * m_p):
    """
    Change in temperature with height
    """
    return (
        (temperature_i - temperature_ip1) / 
        delta_z_i(
            temperature_i, pressure_i, pressure_ip1, g, m_bar=m_bar
        )
    )


def gamma_adiabatic(g, m_bar=2.4 * m_p, n_dof=5):
    return g / c_p(m_bar=m_bar, n_dof=n_dof)


def delta_gamma(
    temperature_i, temperature_ip1, pressure_i, pressure_ip1, g, 
    m_bar=2.4 * m_p, n_dof=5
):
    dg = (
        gamma(temperature_i, temperature_ip1, 
              pressure_i, pressure_ip1, g, m_bar=m_bar) - 
        gamma_adiabatic(g, m_bar=m_bar, n_dof=n_dof)
    )
    return dg


def mixing_length(T_1, g, alpha=1, m_bar=2.4*m_p):
    return alpha * k_B * T_1 / (m_bar * g)


def convective_flux(
    temperature_i, temperature_ip1, pressure_i, pressure_ip1, g, 
    m_bar=2.4 * m_p, n_dof=5, alpha=1
):
    rho = rho_p(pressure_i, pressure_ip1, temperature_i, g, m_bar=m_bar)
    cp = c_p(m_bar=m_bar, n_dof=n_dof)
    lmix = mixing_length(temperature_i, g, alpha, m_bar)
    delta_g = delta_gamma(
        temperature_i, temperature_ip1, pressure_i, pressure_ip1, 
        g, m_bar=m_bar, n_dof=n_dof
    )
    
    if delta_g > 0 * u.K / u.km: 
        return rho * cp * lmix**2 * (g / temperature_i)**0.5 * delta_g**1.5
    return 0 * flux_unit * u.cm


@jit
def emit(
    offline_opacities, temperatures, pressures, lam, F_TOA, g, m_bar=2.4 * m_p.si.value, alpha=1, opacity_grid_temperatures=None
):
    """
    Compute emission spectrum.

    Parameters
    ----------
    opacities : dict
        Opacity database binned to wavelength grid.
    temperatures : ~astropy.units.Quantity
        Temperature grid
    pressures : ~astropy.units.Quantity
        Pressure grid
    lam : ~astropy.units.Quantity
        Wavelength grid
    F_TOA : ~astropy.units.Quantity
        Flux at the top of the atmosphere
    g : ~astropy.units.Quantity
        Surface graivty
    m_bar : ~astropy.units.Quantity
        Mean molecular weight
    n_timesteps : int
        Maximum number of timesteps in iteration for radiative equilibrium
    convergence_thresh : ~astropy.units.Quantity
        When the maximum change in temperature between timesteps is less than
        ``convergence_thresh``, accept this timestep as "converged".

    Returns
    -------
    F_2_up : ~astropy.units.Quantity
        Outgoing flux
    final_temps : ~astropy.units.Quantity
        Final temperature grid
    temperature_history : ~astropy.units.Quantity
        Grid of temperatures with dimensions (n_layers, n_timesteps)
    dtaus : ~numpy.ndarray
        Change in optical depth in final iteration
    """
    n_layers = len(pressures)
    n_wavelengths = len(lam)

    fluxes_up = jnp.zeros((n_layers, n_wavelengths))
    fluxes_down = jnp.zeros((n_layers, n_wavelengths))
    fluxes_down = fluxes_down.at[-1].set(F_TOA)
    
    temps = temperatures

    def body_fun(
        i, fluxes, pressures=pressures, 
        temps=temps, 
        offline_opacities=offline_opacities, g=g,
        opacity_grid_temperatures=opacity_grid_temperatures
    ):
        fluxes_up = fluxes[:fluxes.shape[0]//2]
        fluxes_down = fluxes[fluxes.shape[0]//2:]
        
        p_2 = pressures[i + 1]
        T_2 = temps[i + 1]

        p_1 = pressures[i]
        T_1 = temps[i]

        def outer(
            carry, j, i=i, temps=temps, 
            offline_opacities=offline_opacities, 
            opacity_grid_temperatures=opacity_grid_temperatures
        ):
            """
            Iterate over each lam
            """
            def interp_over_T(
                carry, x, 
                op=offline_opacities[i, :, j], temps=temps, 
                opacity_grid_temperatures=opacity_grid_temperatures
            ):
                """
                Interpolate over temperature at each p and lam (1D)
                """
                interp_T = jnp.interp(x, opacity_grid_temperatures, op)
                return carry, interp_T
            return carry, lax.scan(interp_over_T, 0.0, temps)[1]  
        
        kappa_interp_i = lax.scan(
            outer, 0.0, jnp.arange(len(lam))
        )[1][:, 0]

        delta_tau = delta_tau_i(
            kappa_interp_i, p_1, p_2, g
        )
        
        # Single scattering albedo, Deitrick (2020) Eqn 17
        omega_0 = 0.0
        F_2_down = fluxes_down[i + 1]
        F_1_up = fluxes_up[i]
        F_2_up, F_1_down = propagate_fluxes(
            lam,
            F_1_up, F_2_down, T_1, T_2,
            delta_tau,
            omega_0=omega_0, g_0=0
        )

        return jnp.vstack([
            fluxes_up.at[i + 1].set(F_2_up), 
            fluxes_down.at[i].set(F_1_down)
        ])

    res = lax.fori_loop(1, n_layers, body_fun, jnp.vstack([fluxes_up, fluxes_down]))
    
    fluxes_up = res[:res.shape[0]//2]
    fluxes_down = res[res.shape[0]//2:]
    
    return (
        fluxes_up, fluxes_down
    )
