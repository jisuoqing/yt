import os
import warnings
from functools import partial

import numpy as np

from yt import units
from yt._typing import KnownFieldsT
from yt.fields.field_detector import FieldDetector
from yt.fields.field_info_container import FieldInfoContainer
from yt.frontends.ramses.io import convert_ramses_conformal_time_to_physical_time
from yt.utilities.cython_fortran_utils import FortranFile
from yt.utilities.lib.cosmology_time import t_frw
from yt.utilities.linear_interpolators import BilinearFieldInterpolator
from yt.utilities.logger import ytLogger as mylog
from yt.utilities.physical_constants import (
    boltzmann_constant_cgs,
    mass_hydrogen_cgs,
    mh,
    mp,
)

from .field_handlers import RTFieldFileHandler

b_units = "code_magnetic"
ra_units = "code_length / code_time**2"
rho_units = "code_density"
vel_units = "code_velocity"
pressure_units = "code_pressure"
ener_units = "code_mass * code_velocity**2"
specific_ener_units = "code_velocity**2"
ang_mom_units = "code_mass * code_velocity * code_length"
cooling_function_units = " erg * cm**3 /s"
cooling_function_prime_units = " erg * cm**3 /s/K"
flux_unit = "1 / code_length**2 / code_time"
number_density_unit = "1 / code_length**3"

known_species_masses = {
    sp: mh * v
    for sp, v in [
        ("HI", 1.0),
        ("HII", 1.0),
        ("Electron", 1.0),
        ("HeI", 4.0),
        ("HeII", 4.0),
        ("HeIII", 4.0),
        ("H2I", 2.0),
        ("H2II", 2.0),
        ("HM", 1.0),
        ("DI", 2.0),
        ("DII", 2.0),
        ("HDI", 3.0),
    ]
}

known_species_names = {
    "HI": "H_p0",
    "HII": "H_p1",
    "Electron": "El",
    "HeI": "He_p0",
    "HeII": "He_p1",
    "HeIII": "He_p2",
    "H2I": "H2_p0",
    "H2II": "H2_p1",
    "HM": "H_m1",
    "DI": "D_p0",
    "DII": "D_p1",
    "HDI": "HD_p0",
}

_cool_axes = ("lognH", "logT")  # , "logTeq")
_cool_arrs = (
    ("cooling_primordial", cooling_function_units),
    ("heating_primordial", cooling_function_units),
    ("cooling_compton", cooling_function_units),
    ("heating_compton", cooling_function_units),
    ("cooling_metal", cooling_function_units),
    ("cooling_primordial_prime", cooling_function_prime_units),
    ("heating_primordial_prime", cooling_function_prime_units),
    ("cooling_compton_prime", cooling_function_prime_units),
    ("heating_compton_prime", cooling_function_prime_units),
    ("cooling_metal_prime", cooling_function_prime_units),
    ("mu", None),
    ("abundances", None),
)
_cool_species = (
    "Electron_number_density",
    "HI_number_density",
    "HII_number_density",
    "HeI_number_density",
    "HeII_number_density",
    "HeIII_number_density",
)

_X = 0.76  # H fraction, hardcoded
_Y = 0.24  # He fraction, hardcoded


class RAMSESFieldInfo(FieldInfoContainer):
    known_other_fields: KnownFieldsT = (
        ("Density", (rho_units, ["density"], None)),
        ("x-velocity", (vel_units, ["velocity_x"], None)),
        ("y-velocity", (vel_units, ["velocity_y"], None)),
        ("z-velocity", (vel_units, ["velocity_z"], None)),
        ("Pres_IR", (pressure_units, ["pres_IR", "pressure_IR"], None)),
        ("Pressure", (pressure_units, ["pressure"], None)),
        ("Metallicity", ("", ["metallicity"], None)),
        ("HII", ("", ["H_p1_fraction"], None)),
        ("HeII", ("", ["He_p1_fraction"], None)),
        ("HeIII", ("", ["He_p2_fraction"], None)),
        ("x-acceleration", (ra_units, ["acceleration_x"], None)),
        ("y-acceleration", (ra_units, ["acceleration_y"], None)),
        ("z-acceleration", (ra_units, ["acceleration_z"], None)),
        ("Potential", (specific_ener_units, ["potential"], None)),
        ("B_x_left", (b_units, ["magnetic_field_x_left"], None)),
        ("B_x_right", (b_units, ["magnetic_field_x_right"], None)),
        ("B_y_left", (b_units, ["magnetic_field_y_left"], None)),
        ("B_y_right", (b_units, ["magnetic_field_y_right"], None)),
        ("B_z_left", (b_units, ["magnetic_field_z_left"], None)),
        ("B_z_right", (b_units, ["magnetic_field_z_right"], None)),
    )
    known_particle_fields: KnownFieldsT = (
        ("particle_position_x", ("code_length", [], None)),
        ("particle_position_y", ("code_length", [], None)),
        ("particle_position_z", ("code_length", [], None)),
        ("particle_velocity_x", (vel_units, [], None)),
        ("particle_velocity_y", (vel_units, [], None)),
        ("particle_velocity_z", (vel_units, [], None)),
        ("particle_mass", ("code_mass", [], None)),
        ("particle_identity", ("", ["particle_index"], None)),
        ("particle_refinement_level", ("", [], None)),
        ("particle_birth_time", ("code_time", ["age"], None)),
        ("conformal_birth_time", ("", [], None)),
        ("particle_metallicity", ("", [], None)),
        ("particle_family", ("", [], None)),
        ("particle_tag", ("", [], None)),
        # sink field parameters
        ("particle_mass", ("code_mass", [], None)),
        ("particle_angular_momentum_x", (ang_mom_units, [], None)),
        ("particle_angular_momentum_y", (ang_mom_units, [], None)),
        ("particle_angular_momentum_z", (ang_mom_units, [], None)),
        ("particle_formation_time", ("code_time", [], None)),
        ("particle_accretion_rate", ("code_mass/code_time", [], None)),
        ("particle_delta_mass", ("code_mass", [], None)),
        ("particle_rho_gas", (rho_units, [], None)),
        ("particle_cs**2", (vel_units, [], None)),
        ("particle_etherm", (ener_units, [], None)),
        ("particle_velocity_x_gas", (vel_units, [], None)),
        ("particle_velocity_y_gas", (vel_units, [], None)),
        ("particle_velocity_z_gas", (vel_units, [], None)),
        ("particle_mass_bh", ("code_mass", [], None)),
        ("particle_level", ("", [], None)),
        ("particle_radius_star", ("code_length", [], None)),
    )

    known_sink_fields: KnownFieldsT = (
        ("particle_position_x", ("code_length", [], None)),
        ("particle_position_y", ("code_length", [], None)),
        ("particle_position_z", ("code_length", [], None)),
        ("particle_velocity_x", (vel_units, [], None)),
        ("particle_velocity_y", (vel_units, [], None)),
        ("particle_velocity_z", (vel_units, [], None)),
        ("particle_mass", ("code_mass", [], None)),
        ("particle_identifier", ("", ["particle_index"], None)),
        ("particle_birth_time", ("code_time", ["age"], None)),
        ("BH_real_accretion", ("code_mass/code_time", [], None)),
        ("BH_bondi_accretion", ("code_mass/code_time", [], None)),
        ("BH_eddington_accretion", ("code_mass/code_time", [], None)),
        ("BH_esave", (ener_units, [], None)),
        ("gas_spin_x", (ang_mom_units, [], None)),
        ("gas_spin_y", (ang_mom_units, [], None)),
        ("gas_spin_z", (ang_mom_units, [], None)),
        ("BH_spin_x", ("", [], None)),
        ("BH_spin_y", ("", [], None)),
        ("BH_spin_z", ("", [], None)),
        ("BH_spin", (ang_mom_units, [], None)),
        ("BH_efficiency", ("", [], None)),
    )

    def setup_particle_fields(self, ptype):
        super().setup_particle_fields(ptype)

        def star_age_from_conformal_cosmo(field, data):
            conformal_age = data[ptype, "conformal_birth_time"]
            birth_time = convert_ramses_conformal_time_to_physical_time(
                data.ds, conformal_age
            )
            return data.ds.current_time - birth_time

        def star_age_from_physical_cosmo(field, data):
            H0 = float(
                data.ds.quan(data.ds.hubble_constant * 100, "km/s/Mpc").to("1/Gyr")
            )
            times = data[ptype, "conformal_birth_time"].value
            time_tot = float(t_frw(data.ds, 0) * H0)
            birth_time = (time_tot + times) / H0
            t_out = float(data.ds.current_time.to("Gyr"))
            return data.apply_units(t_out - birth_time, "Gyr")

        def star_age(field, data):
            formation_time = data[ptype, "particle_birth_time"]
            return data.ds.current_time - formation_time

        if self.ds.cosmological_simulation and self.ds.use_conformal_time:
            fun = star_age_from_conformal_cosmo
        elif self.ds.cosmological_simulation:
            fun = star_age_from_physical_cosmo
        else:
            fun = star_age

        self.add_field(
            (ptype, "star_age"),
            sampling_type="particle",
            function=fun,
            units=self.ds.unit_system["time"],
        )

    def setup_fluid_fields(self):
        def _temperature_over_mu(field, data):
            rv = data["gas", "pressure"] / data["gas", "density"]
            rv *= mass_hydrogen_cgs / boltzmann_constant_cgs
            return rv

        self.add_field(
            ("gas", "temperature_over_mu"),
            sampling_type="cell",
            function=_temperature_over_mu,
            units=self.ds.unit_system["temperature"],
        )
        found_cooling_fields = self.create_cooling_fields()

        if found_cooling_fields:

            def _temperature(field, data):
                return data["gas", "temperature_over_mu"] * data["gas", "mu"]

        else:

            def _temperature(field, data):
                if not isinstance(data, FieldDetector):
                    warnings.warn(
                        "Trying to calculate temperature but the cooling tables "
                        "couldn't be found or read. yt will return T/µ instead of "
                        "T — this is equivalent to assuming µ=1.0. To suppress this, "
                        "derive the temperature from temperature_over_mu with "
                        "some values for mu.",
                        category=RuntimeWarning,
                        stacklevel=1,
                    )
                return data["gas", "temperature_over_mu"]

        self.add_field(
            ("gas", "temperature"),
            sampling_type="cell",
            function=_temperature,
            units=self.ds.unit_system["temperature"],
        )

        self.species_names = [
            known_species_names[fn]
            for ft, fn in self.field_list
            if fn in known_species_names
        ]

        # See if we need to load the rt fields
        rt_flag = RTFieldFileHandler.any_exist(self.ds)
        if rt_flag:  # rt run
            self.create_rt_fields()

        # Load magnetic fields
        if ("gas", "magnetic_field_x_left") in self:
            self.create_magnetic_fields()

        # Potential field
        if ("gravity", "Potential") in self:
            self.create_gravity_fields()

    def create_gravity_fields(self):
        def potential_energy(field, data):
            return data["gas", "potential"] * data["gas", "cell_mass"]

        self.add_field(
            ("gas", "potential_energy"),
            sampling_type="cell",
            function=potential_energy,
            units=self.ds.unit_system["energy"],
        )

    def create_magnetic_fields(self):
        # Calculate cell-centred magnetic fields from face-centred
        def mag_field(ax):
            def _mag_field(field, data):
                return (
                    data["gas", f"magnetic_field_{ax}_left"]
                    + data["gas", f"magnetic_field_{ax}_right"]
                ) / 2

            return _mag_field

        for ax in self.ds.coordinates.axis_order:
            self.add_field(
                ("gas", f"magnetic_field_{ax}"),
                sampling_type="cell",
                function=mag_field(ax),
                units=self.ds.unit_system["magnetic_field_cgs"],
            )

        def _divB(field, data):
            """Calculate magnetic field divergence"""
            out = np.zeros_like(data["gas", "magnetic_field_x_right"])
            for ax in data.ds.coordinates.axis_order:
                out += (
                    data["gas", f"magnetic_field_{ax}_right"]
                    - data["gas", f"magnetic_field_{ax}_left"]
                )
            return out / data["gas", "dx"]

        self.add_field(
            ("gas", "magnetic_field_divergence"),
            sampling_type="cell",
            function=_divB,
            units=self.ds.unit_system["magnetic_field_cgs"]
            / self.ds.unit_system["length"],
        )

    def create_rt_fields(self):
        self.ds.fluid_types += ("rt",)
        p = RTFieldFileHandler.get_rt_parameters(self.ds).copy()
        p.update(self.ds.parameters)
        ngroups = p["nGroups"]
        # Make sure rt_c_frac is at least as long as the number of levels in
        # the simulation. Pad with either 1 (default) when using level-dependent
        # reduced speed of light, otherwise pad with a constant value
        if len(p["rt_c_frac"]) == 1:
            pad_value = p["rt_c_frac"][0]
        else:
            pad_value = 1
        rt_c_frac = np.pad(
            p["rt_c_frac"],
            (0, max(0, self.ds.max_level - len(["rt_c_frac"]) + 1)),
            constant_values=pad_value,
        )

        rt_c = rt_c_frac * units.c / (p["unit_l"] / p["unit_t"])
        dens_conv = (p["unit_np"] / rt_c).value / units.cm**3

        ########################################
        # Adding the fields in the hydro_* files
        def _temp_IR(field, data):
            rv = data["gas", "pres_IR"] / data["gas", "density"]
            rv *= mass_hydrogen_cgs / boltzmann_constant_cgs
            return rv

        self.add_field(
            ("gas", "temp_IR"),
            sampling_type="cell",
            function=_temp_IR,
            units=self.ds.unit_system["temperature"],
        )

        def _species_density(field, data, species: str):
            return data["gas", f"{species}_fraction"] * data["gas", "density"]

        def _species_mass(field, data, species: str):
            return data["gas", f"{species}_density"] * data["index", "cell_volume"]

        for species in ["H_p1", "He_p1", "He_p2"]:
            self.add_field(
                ("gas", species + "_density"),
                sampling_type="cell",
                function=partial(_species_density, species=species),
                units=self.ds.unit_system["density"],
            )

            self.add_field(
                ("gas", species + "_mass"),
                sampling_type="cell",
                function=partial(_species_mass, species=species),
                units=self.ds.unit_system["mass"],
            )

        ########################################
        # Adding the fields in the rt_ files
        def gen_pdens(igroup):
            def _photon_density(field, data):
                # The photon density depends on the possibly level-dependent conversion factor.
                ilvl = data["index", "grid_level"].astype("int64")
                dc = dens_conv[ilvl]
                rv = data["ramses-rt", f"Photon_density_{igroup + 1}"] * dc
                return rv

            return _photon_density

        for igroup in range(ngroups):
            self.add_field(
                ("rt", f"photon_density_{igroup + 1}"),
                sampling_type="cell",
                function=gen_pdens(igroup),
                units=self.ds.unit_system["number_density"],
            )

        flux_conv = p["unit_pf"] / units.cm**2 / units.s
        flux_unit = (
            1 / self.ds.unit_system["time"] / self.ds.unit_system["length"] ** 2
        ).units

        def gen_flux(key, igroup):
            def _photon_flux(field, data):
                rv = data["ramses-rt", f"Photon_flux_{key}_{igroup + 1}"] * flux_conv
                return rv

            return _photon_flux

        for key in "xyz":
            for igroup in range(ngroups):
                self.add_field(
                    ("rt", f"photon_flux_{key}_{igroup + 1}"),
                    sampling_type="cell",
                    function=gen_flux(key, igroup),
                    units=flux_unit,
                )

    def create_cooling_fields(self) -> bool:
        "Create cooling fields from the cooling files. Return True if successful."
        num = int(self.ds.basename.split(".")[0].split("_")[1])
        filename = os.path.join(self.ds.directory, f"cooling_{num:05}.out")

        if not os.path.exists(filename):
            mylog.warning("This output has no cooling fields")
            return False

        # Function to create the cooling fields
        def _create_field(name, interp_object, unit):
            def _func(field, data):
                shape = data["gas", "temperature_over_mu"].shape
                # Ramses assumes a fraction X of Hydrogen within the non-metal gas.
                # It has to be corrected by metallicity.
                Z = data["gas", "metallicity"]
                nH = ((1 - _Y) * (1 - Z) * data["gas", "density"] / mh).to("cm**-3")
                if data.ds.self_shielding:
                    boost = np.maximum(np.exp(-nH / 0.01), 1e-20)
                else:
                    boost = 1
                d = {
                    "lognH": np.log10(nH / boost).ravel(),
                    "logT": np.log10(data["gas", "temperature_over_mu"]).ravel(),
                }
                rv = interp_object(d).reshape(shape)
                if name[-1] != "mu":
                    rv = 10 ** interp_object(d).reshape(shape)
                cool = data.ds.arr(rv, unit)
                if "metal" in name[-1].split("_"):
                    cool = (
                        cool * data["gas", "metallicity"] / 0.02
                    )  # Ramses uses Zsolar=0.02
                elif "compton" in name[-1].split("_"):
                    cool = data.ds.arr(rv, unit + "/cm**3")
                    cool = (
                        cool / data["gas", "number_density"]
                    )  # Compton cooling/heating is written to file in erg/s
                return cool

            self.add_field(name=name, sampling_type="cell", function=_func, units=unit)

        # Load cooling files
        avals = {}
        tvals = {}
        with FortranFile(filename) as fd:
            n1, n2 = fd.read_vector("i")
            for ax in _cool_axes:
                avals[ax] = fd.read_vector("d")
            for i, (tname, unit) in enumerate(_cool_arrs):
                var = fd.read_vector("d")
                if var.size == n1 and i == 0:
                    # If this case occurs, the cooling files were produced pre-2010 in
                    # a format that is no longer supported
                    mylog.warning(
                        "This cooling file format is no longer supported. "
                        "Cooling field loading skipped."
                    )
                    return False
                if var.size == n1 * n2:
                    tvals[tname] = {
                        "data": var.reshape((n1, n2), order="F"),
                        "unit": unit,
                    }
                else:
                    var = var.reshape((n1, n2, var.size // (n1 * n2)), order="F")
                    for i in range(var.shape[-1]):
                        tvals[_cool_species[i]] = {
                            "data": var[:, :, i],
                            "unit": "1/cm**3",
                        }

        # Add the mu field first, as it is needed for the number density
        interp = BilinearFieldInterpolator(
            tvals["mu"]["data"],
            (avals["lognH"], avals["logT"]),
            ["lognH", "logT"],
            truncate=True,
        )
        _create_field(("gas", "mu"), interp, "dimensionless")

        # Add the number density field, based on mu
        def _number_density(field, data):
            return data["gas", "density"] / mp / data["gas", "mu"]

        self.add_field(
            name=("gas", "number_density"),
            sampling_type="cell",
            function=_number_density,
            units=number_density_unit,
        )

        # Add the cooling and heating fields, which need the number density field
        for key in tvals:
            if key != "mu":
                interp = BilinearFieldInterpolator(
                    tvals[key]["data"],
                    (avals["lognH"], avals["logT"]),
                    ["lognH", "logT"],
                    truncate=True,
                )
                _create_field(("gas", key), interp, tvals[key]["unit"])

        # Add total cooling and heating fields
        def _all_cool(field, data):
            return (
                data["gas", "cooling_primordial"]
                + data["gas", "cooling_metal"]
                + data["gas", "cooling_compton"]
            )

        def _all_heat(field, data):
            return data["gas", "heating_primordial"] + data["gas", "heating_compton"]

        self.add_field(
            name=("gas", "cooling_total"),
            sampling_type="cell",
            function=_all_cool,
            units=cooling_function_units,
        )
        self.add_field(
            name=("gas", "heating_total"),
            sampling_type="cell",
            function=_all_heat,
            units=cooling_function_units,
        )

        # Add net cooling fields
        def _net_cool(field, data):
            return data["gas", "cooling_total"] - data["gas", "heating_total"]

        self.add_field(
            name=("gas", "cooling_net"),
            sampling_type="cell",
            function=_net_cool,
            units=cooling_function_units,
        )

        return True
