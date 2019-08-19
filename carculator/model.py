"""
.. module: model.py

"""
from pathlib import Path
from inspect import currentframe, getframeinfo
import numpy as np
from .energy_consumption import EnergyConsumptionModel
from bw2io import ExcelImporter
from bw2io.export.excel import safe_filename,\
    xlsxwriter, CSVFormatter,\
    create_valid_worksheet_name
import uuid


DEFAULT_MAPPINGS = {
    "electric": {"BEV", "PHEV-e"},
    "combustion": {"ICEV-p", "HEV-p", "PHEV-c", "ICEV-g", "ICEV-d"},
    "combustion_wo_cng": {"ICEV-p", "HEV-p", "PHEV-c", "ICEV-d"},
    "pure_combustion": {"ICEV-p", "ICEV-g", "ICEV-d"},
    "petrol": {"ICEV-p", "HEV-p", "PHEV-c"},
    "cng": {"ICEV-g"},
    "fuel_cell": {"FCEV"},
    "hybrid": {"PHEV-c", "PHEV-e"},
    "combustion_hybrid": {"PHEV-c"},
    "electric_hybrid": {"PHEV-e"},
    "diesel": {"ICEV-d"},
    "battery": {"BEV"},
}


def finite(array, mask_value=0):
    return np.where(np.isfinite(array), array, mask_value)


class CarModel:

    """
    This class represents the entirety of the vehicles considered, with useful attributes.

    :ivar array: multi-dimensional numpy-like array that contains parameters' value(s)
    :vartype array: xarray.DataArray
    :ivar mappings: Dictionary with names correspondence
    :vartype mappings: dict
    :ivar ecm: instance of EnergyConsumptionModel class for a given driving cycle
    :vartype ecm: coarse.energy_consumption.EnergyConsumptionModel

    """

    def __init__(self, array, mappings=None, cycle=None):

        self.array = array
        self.mappings = mappings or DEFAULT_MAPPINGS

        if cycle is None:
            self.ecm = EnergyConsumptionModel('WLTC')
        else:
            self.ecm = EnergyConsumptionModel(cycle)


    def __call__(self, key):
        """
        This method fixes a dimension of the `array` attribute given a powertrain technology selected.

        Set up this class as a context manager, so we can have some nice syntax

        :Example:

        with class('some powertrain') as cpm:
            cpm['something']. <- Will be filtered for the correct powertrain

        On with block exit, this filter is cleared
        https://stackoverflow.com/a/10252925/164864


        :param key: A powertrain type, e.g., "FCEV"
        :type key: str
        :return: An instance of `array` filtered after the powertrain selected.

        """
        self.__cache = self.array
        self.array = self.array.sel(powertrain=key)
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.array = self.__cache
        del self.__cache


    def __getitem__(self, key):
        """
        Make class['foo'] automatically filter for the parameter 'foo'
        Makes the model code much cleaner

        :param key: Parameter name
        :type key: str
        :return: `array` filtered after the parameter selected
        """

        return self.array.sel(parameter=key)

    def __setitem__(self, key, value):
        self.array.loc[{"parameter": key}] = value

    # Make it easier/more flexible to filter by powertrain types
    def __getattr__(self, key):
        if key in self.mappings:
            return self.mappings[key]
        else:
            return super().__getattr__(key)



    def set_all(self):
        """
        This method runs a series of other methods to obtain the tank-to-wheel energy requirement, efficiency
        of the car, costs, etc.

        """

        """
        set_component_masses(), set_car_masses() and set_power_parameters() are interdependent.
        `powertrain_mass` depends on `power`, `curb_mass` is affected by changes in `powertrain_mass`,
        `combustion engine mass` and `electric engine mass`, and `power` is a function of `curb_mass`.
        The current solution is to loop through the methods until the increment in driving mass is
        inferior to 0.1%.
        
        
        """
        # TODO: Converging towards a satisfying curb mass is taking too long! Needs to be optimized.



        diff = 1.0

        while diff > .01:
            old_driving_mass = self['driving mass'].sum().values

            self.set_car_masses()


            self.set_power_parameters()
            self.set_component_masses()
            self.set_battery_properties()
            self.set_battery_fuel_cell_replacements()
            self.set_recuperation()
            self.set_fuel_cell_parameters()
            self.set_energy_stored_properties()


            diff = (self['driving mass'].sum().values-old_driving_mass)/self['driving mass'].sum()

        self.set_auxiliaries()
        self.set_ttw_efficiency()
        self.calculate_ttw_energy()

        self.set_range()


        self.set_electric_utility_factor()
        self.set_electricity_consumption()

        self.set_costs()


        self.calculate_lci()
        self.create_PHEV()
        self.drop_hybrid()


    def drop_hybrid(self):
        """
        This method drops the powertrain `PHEV-c` and `PHEV-e` as they were only used to create the `PHEV` powertrain.
        :return:
        """
        self.array = self.array.sel(powertrain=['ICEV-p', 'ICEV-d', 'ICEV-g', 'PHEV', 'FCEV','BEV', 'HEV-p'])


    def set_electricity_consumption(self):
        """
        This method calculates the total electricity consumption for BEV and plugin-hybrid vehicles

        """

        for pt in self.electric:
            with self(pt) as cpm:
                cpm['electricity consumption'] = (cpm['TtW energy'] / cpm['battery discharge efficiency']) / 3600

    def calculate_ttw_energy(self):
        """
        This  method calculates the energy required to operate auxiliary services as well
        as to move the car. The sum is stored in `array` under the label "TtW energy".

        """
        aux_energy = self.ecm.aux_energy_per_km(self["auxiliary power demand"])

        for pt in self.pure_combustion:
            with self(pt) as cpm:
                aux_energy.loc[{"powertrain": pt}] /= cpm['engine efficiency']
        for pt in self.fuel_cell:
            with self(pt) as cpm:
                aux_energy.loc[{"powertrain": pt}] /= cpm['fuel cell system efficiency']

        self['auxiliary energy'] = aux_energy


        motive_energy = self.ecm.motive_energy_per_km(
            driving_mass=self["driving mass"],
            rr_coef=self["rolling resistance coefficient"],
            drag_coef=self["aerodynamic drag coefficient"],
            frontal_area=self["frontal area"],
            ttw_efficiency=self["TtW efficiency"],
            recuperation_efficiency=self["recuperation efficiency"],
            motor_power=self["electric power"],
        ).sum(axis=-1)

        self.motive_energy = motive_energy


        self["TtW energy"] = aux_energy + motive_energy

    def set_fuel_cell_parameters(self):
        """
        Specific setup for fuel cells, which are mild hybrids.

        Must be called after ``.set_power_parameters``."""
        for pt in self.fuel_cell:
            with self(pt):
                self["fuel cell system efficiency"] = (
                    self["fuel cell stack efficiency"]
                    / self["fuel cell own consumption"]
                )

                self["fuel cell power"] = (
                    self["power"]
                    * self["fuel cell power share"]
                    * self["fuel cell own consumption"]
                )
                # our basic fuel cell mass is based on a car fuel cell with 800 mW/cm2 and 0.51 kg/kW
                self["fuel cell stack mass"] = (
                    0.51
                    * self["fuel cell power"]
                    * 800
                    / self["fuel cell power area density"]
                )
                self["fuel cell ancillary BoP mass"] = (
                    self["fuel cell power"]
                    * self["fuel cell ancillary BoP mass per power"]
                )
                self["fuel cell essential BoP mass"] = (
                    self["fuel cell power"]
                    * self["fuel cell essential BoP mass per power"]
                )

                self["battery power"] = self["fuel cell power"] * (
                    1 - self["fuel cell power share"]
                )
                self["battery cell mass"] = (
                    self["battery power"] / self["battery cell power density"]
                )
                self["battery BoP mass"] = self["battery cell mass"] * (
                    1 - self["battery cell mass share"]
                )

                self["oxidation energy stored"] = self["fuel mass"] * 120 / 3.6  # kWh
                self["fuel tank mass"] = (
                    self["oxidation energy stored"] * self["H2 tank mass per energy"]
                )

    def set_auxiliaries(self):
        """
        Calculates the power needed to operate the auxiliary services of the vehicle (heating, cooling).

        The demand for heat and cold are expressed as a fraction of the heating and cooling capacities
        Auxiliary power demand (W) = Base auxiliary power (W) +
        (Heating demand (dimensionless, between 0 and 1) * Heating power (W)) +
        (Cooling demand (dimensionless, between 0 and 1) * Cooling power (W))


        """
        self["auxiliary power demand"] = (
            self["auxilliary power base demand"]
            + self["heating thermal demand"] * self["heating energy consumption"]
            + self["cooling thermal demand"] * self["cooling energy consumption"]
        )

    def set_recuperation(self):
        self["recuperation efficiency"] = (
            self["drivetrain efficiency"] * self["battery charge efficiency"]
        )

    def set_battery_fuel_cell_replacements(self):
        """
        This methods calculates the fraction of the replacement battery needed to match the vehicle lifetime.

        :Example:
            car lifetime = 200000 (km)
            battery lifetime = 190000 (km)
            replacement battery = 0.05
            
        :note: It is debatable whether this is realistic or not. Car oners may not decide to invest in a new
        battery if the remaining lifetime of the car is only 10000 km. Also, a battery lifetime may be expressed
        in other terms, e.g., charging cycles.


        """
        # Here we assume that we can use fractions of a battery/fuel cell
        # (averaged across the fleet)
        self['battery lifetime replacements'] = finite(np.clip(
            (self['lifetime kilometers'] / self['battery lifetime kilometers']) - 1,
            0,
            None
        ))
        self['fuel cell lifetime replacements'] = finite(np.clip(
            (self['lifetime kilometers'] / self['fuel cell lifetime kilometers']) - 1,
            0,
            None
        ))

    def set_car_masses(self):
        """Define ``curb mass``, ``driving mass``, and ``total cargo mass``.

        * `curb mass <https://en.wikipedia.org/wiki/Curb_weight>`__ is the mass of the vehicle and fuel, without people
        or cargo.
        * ``total cargo mass`` is the mass of the cargo and passengers.
        * ``driving mass`` is the ``curb mass`` plus ``total cargo mass``.

        """
        self["curb mass"] = self["glider base mass"] * (1 - self["lightweighting"])

        curb_mass_includes = [
            "fuel mass",
            "charger mass",
            "converter mass",
            "inverter mass",
            "power distribution unit mass",
            # Updates with set_components_mass
            "combustion engine mass",
            # Updates with set_components_mass
            "electric engine mass",
            # Updates with set_components_mass
            "powertrain mass",
            "fuel cell stack mass",
            "fuel cell ancillary BoP mass",
            "fuel cell essential BoP mass",
            "battery cell mass",
            "battery BoP mass",
            "fuel tank mass",
        ]
        self["curb mass"] += self[curb_mass_includes].sum(axis=2)

        self["total cargo mass"] = (
            self["average passengers"] * self["average passenger mass"]
            + self["cargo mass"]
        )
        self["driving mass"] = self["curb mass"] + self["total cargo mass"]

    def set_power_parameters(self):
        """Set electric and combustion motor powers based on input parameter ``power to mass ratio``."""
        # Convert from W/kg to kW
        self["power"] = self["power to mass ratio"] * self["curb mass"] / 1000
        self["combustion power"] = self["power"] * self["combustion power share"]
        self["electric power"] = self["power"] * (1 - self["combustion power share"])

    def set_component_masses(self):
        self["combustion engine mass"] = (
            self["combustion power"] * self["combustion mass per power"]
            + self["combustion fixed mass"]
        )
        self["electric engine mass"] = (
            self["electric power"] * self["electric mass per power"]
            + self["electric fixed mass"]
        )
        self["powertrain mass"] = (
            self["power"] * self["powertrain mass per power"]
            + self["powertrain fixed mass"]
        )

    def set_electric_utility_factor(self):

        with self('PHEV-e') as cpm:
            cpm['electric utility factor'] = (1- np.exp(-0.01147 * cpm['range'])) ** 1.186185

    def create_PHEV(self):
        """ PHEV is the range-weighted average between PHEV-c and PHEV-e.
        """
        self.array.loc[:, 'PHEV', :, :, :] = (self.array.loc[:, 'PHEV-e', :, :, :]
            * self.array.loc[:, 'PHEV-e', 'electric utility factor', :, :])\
            +(self.array.loc[:, 'PHEV-c', :, :, :] * (1-self.array.loc[:, 'PHEV-e', 'electric utility factor', :, :]))

        #self.array.loc[:, 'PHEV', 'range', :, :] = self.array.loc[:, 'PHEV-c', 'range', :, :] +\
        #                                           self.array.loc[:, 'PHEV-e', 'range', :, :]

    def set_battery_properties(self):
        for pt in ["ICEV-p", "HEV-p", "ICEV-g", "ICEV-d"]:
            with self(pt) as cpm:
                cpm["battery power"] = cpm["electric power"]
                cpm["battery cell mass"] = (
                    cpm["battery power"] / cpm["battery cell power density"]
                )
                cpm["battery BoP mass"] = cpm["battery cell mass"] * (
                    1 - cpm["battery cell mass share"]
                )
        for pt in ['BEV', 'PHEV-c', 'PHEV-e']:
            with self(pt) as cpm:
                cpm["battery cell mass"] = (
                    cpm["energy battery mass"] * cpm["battery cell mass share"]
                )
                cpm["battery BoP mass"] = cpm["energy battery mass"] * (
                    1 - cpm["battery cell mass share"]
                )

    def set_range(self):

        for pt in self.petrol:
            with self(pt) as cpm:
                # Assume 42.4 MJ/kg of gasoline, convert to kWh
                cpm['range'] = (cpm["fuel mass"] * 42.4 * 1000) / cpm['TtW energy']

        for pt in self.diesel:
            with self(pt) as cpm:
                # Assume 48 MJ/kg of gasoline, convert to kWh
                cpm['range'] = (cpm["fuel mass"] * 48 * 1000) / cpm['TtW energy']

        for pt in self.cng:
            with self(pt) as cpm:
                # Assume 55.5 MJ/kg of gasoline, convert to kWh
                cpm['range'] = (cpm["fuel mass"] * 55.5 * 1000) / cpm['TtW energy']

        for pt in self.electric:
            with self(pt) as cpm:
                cpm['range'] = (cpm["electric energy stored"] * cpm["battery DoD"] * 3.6 * 1000) / cpm['TtW energy']

        with self('FCEV') as cpm:
            cpm['range'] = (cpm["fuel mass"] * 120 * 1000) / cpm['TtW energy']

    def set_energy_stored_properties(self):

        for pt in self.petrol:
            with self(pt) as cpm:
                # Assume 42.4 MJ/kg of gasoline, convert to kWh
                cpm["oxidation energy stored"] = cpm["fuel mass"] * 42.4 / 3.6
                cpm["fuel tank mass"] = (
                    cpm["oxidation energy stored"] * cpm["fuel tank mass per energy"]
                )

        for pt in self.diesel:
            with self(pt) as cpm:
                # Assume 48 MJ/kg of gasoline, convert to kWh
                cpm["oxidation energy stored"] = cpm["fuel mass"] * 48 / 3.6
                cpm["fuel tank mass"] = (
                    cpm["oxidation energy stored"] * cpm["fuel tank mass per energy"]
                )

        for pt in self.cng:
            with self(pt) as cpm:
                # Assume 55.5 MJ/kg of gasoline, convert to kWh
                cpm["oxidation energy stored"] = cpm["fuel mass"] * 55.5 / 3.6
                cpm["fuel tank mass"] = (
                    cpm["oxidation energy stored"] * cpm["CNG tank mass slope"]
                    + cpm["CNG tank mass intercept"]
                )

        for pt in self.battery:
            with self(pt) as cpm:
                cpm["electric energy stored"] = (
                    cpm["battery cell mass"] * cpm["battery cell energy density"]
                )


        for pt in self.electric_hybrid:
            with self(pt) as cpm:
                cpm["electric energy stored"] = (
                    cpm["battery cell mass"] * cpm["battery cell energy density"]
                )
                # Assume 42.4 MJ/kg of gasoline
                cpm["fuel tank mass"] = (
                    cpm["fuel mass"] * 42.4 / 3.6 * cpm["fuel tank mass per energy"]
                )




        self["battery cell production electricity"] = (
            self["battery cell production energy"]
            * self["battery cell production energy electricity share"]
        )
        self["battery cell production heat"] = (
            self["battery cell production energy"]
            - self["battery cell production electricity"]
        )

    def set_costs(self):
        self["glider cost"] = (
            self["glider base mass"] * self["glider cost slope"]
            + self["glider cost intercept"]
        )
        self["lightweighting cost"] = (
            self["glider base mass"]
            * self["lightweighting"]
            * self["glider lightweighting cost per kg"]
        )
        self["electric powertrain cost"] = (
            self["electric powertrain cost per kW"] * self["electric power"]
        )
        self["combustion powertrain cost"] = (
            self["combustion power"] * self["combustion powertrain cost per kW"]
        )
        self["fuel cell cost"] = self["fuel cell power"] * self["fuel cell cost per kW"]
        self["power battery cost"] = (
            self["battery power"] * self["power battery cost per kW"]
        )
        self["energy battery cost"] = (
            self["energy battery cost per kWh"]
            * self["battery cell mass"]
            * self["battery cell energy density"]
        )
        self["fuel tank cost"] = self["fuel tank cost per kg"] * self["fuel mass"]
        # Per km
        self["energy cost"] = self["energy cost per kWh"] * self["TtW energy"] / 3600

        # For battery, need to divide cost of electricity in battery by efficiency of charging
        for pt in self.battery:
            with self(pt):
                self["energy cost"] /= self["battery charge efficiency"]

        self["component replacement cost"] = (
            self["energy battery cost"] * self["battery lifetime replacements"]
            + self["fuel cell cost"] * self["fuel cell lifetime replacements"]
        )

        to_markup = [
            "combustion powertrain cost",
            "component replacement cost",
            "electric powertrain cost",
            "energy battery cost",
            "fuel cell cost",
            "fuel tank cost",
            "glider cost",
            "lightweighting cost",
            "power battery cost",
        ]

        self[to_markup] *= self["markup factor"]


        # calculate costs per km:
        self["lifetime"] = self["lifetime kilometers"] / self["kilometers per year"]
        i = self["interest rate"]
        amortisation_factor = i + (i / ((1 + i) ** self["lifetime"] - 1))

        purchase_cost_list = [
            "battery onboard charging infrastructure cost",
            "combustion exhaust treatment cost",
            "combustion powertrain cost",
            "electric powertrain cost",
            "energy battery cost",
            "fuel cell cost",
            "fuel tank cost",
            "glider cost",
            "heat pump cost",
            "lightweighting cost",
            "power battery cost",
        ]

        self['purchase cost'] = self[purchase_cost_list].sum(axis=2)

        # per km
        self["amortised purchase cost"] = (
            self["purchase cost"] * amortisation_factor / self["kilometers per year"]
        )
        # per km
        self["maintenance cost"] = (
            self["maintenance cost per glider cost"]
            * self["glider cost"]
            / self["kilometers per year"]
        )

        # simple assumption that component replacement occurs at half of life.
        self["amortised component replacement cost"] = (
            self["component replacement cost"]
            * ((1 - self["interest rate"]) ** self["lifetime"] / 2)
            * amortisation_factor
            / self["kilometers per year"]
        )



        self["total cost per km"] = (
            self["energy cost"]
            + self["amortised purchase cost"]
            + self["maintenance cost"]
            + self["amortised component replacement cost"]
        )

    def set_ttw_efficiency(self):
        _ = lambda array: np.where(array == 0, 1, array)

        self["TtW efficiency"] = (
            _(self["battery discharge efficiency"])
            * _(self["fuel cell system efficiency"])
            * self["drivetrain efficiency"]
            * self["engine efficiency"]
        )

    def calculate_lci(self):
        """
        Calculate material and energy requirements per vehicle-km.
        :return:
        """

        self['lci_glider'] = self['glider base mass'] / self['lifetime kilometers']
        self['lci_glider_lightweighting'] = self['lightweighting'] / self['lifetime kilometers']
        self['lci_car_maintenance'] = self['curb mass'] / 1600 / 150000

        # Glider
        for pt in self.combustion:
            with self(pt) as cpm:
                cpm['lci_electric_EoL'] = cpm['curb mass'] * (1 - cpm['combustion power share']) / 1180 / cpm['lifetime kilometers'] *-1
                cpm['lci_combustion_EoL'] = cpm['curb mass'] * cpm['combustion power share'] / 1600 / cpm[
                    'lifetime kilometers'] *-1

        for pt in self.electric:
            with self(pt) as cpm:
                cpm['lci_electric_EoL'] = cpm['curb mass'] / 1180 / cpm['lifetime kilometers'] *-1


        # Powertrain
        for pt in self.electric:
            with self(pt) as p:
                p['lci_charger'] = p['charger mass'] / p['lifetime kilometers']

        for pt in ["BEV", "PHEV-c","PHEV-e", "FCEV"]:
            with self(pt) as p:
                p['lci_converter'] = p['converter mass'] / p['lifetime kilometers']

        self['lci_electric_engine'] = self['electric engine mass'] / self['lifetime kilometers']

        for pt in ["BEV", "PHEV-c","PHEV-e", "FCEV", 'HEV-p']:
            with self(pt) as p:
                p['lci_inverter'] = p['inverter mass'] / p['lifetime kilometers']
                p['lci_power_distribution_unit'] = p['power distribution unit mass'] / p['lifetime kilometers']

        l_elec_pt = ['charger mass','converter mass','inverter mass','power distribution unit mass',
            'electric engine mass', 'fuel cell stack mass', 'fuel cell ancillary BoP mass', 'fuel cell essential BoP mass',
                     'battery cell mass','battery BoP mass']

        self['lci_electric_powertrain_EoL'] = self[l_elec_pt].sum(axis=2) / self['lifetime kilometers'] *-1

        self['lci_engine'] = (self[['combustion engine mass','electric engine mass']].sum(axis=2)) / self['lifetime kilometers']

        self['lci_rest_of_powertrain'] = self['powertrain mass'] / self['lifetime kilometers']

        with self('FCEV') as pt:
            pt['lci_fuel_cell_ancillary_BoP'] = pt['fuel cell ancillary BoP mass'] / pt['lifetime kilometers']
            pt['lci_fuel_cell_essential_BoP'] = pt['fuel cell essential BoP mass'] / pt['lifetime kilometers']
            pt['lci_fuel_cell_stack'] = pt['fuel cell stack mass'] / pt['lifetime kilometers']

        # Energy storage

        self['lci_battery_BoP'] = self['battery BoP mass'] * (1 + self['battery lifetime replacements']) / self['lifetime kilometers']
        self['lci_battery_cell'] = self['battery cell mass'] * (1 + self['fuel cell lifetime replacements']) / self[
            'lifetime kilometers']
        self['lci_battery_production_electricity_correction'] = -28 * self['battery cell mass'] * (1+ self['battery lifetime replacements'])\
            / self['lifetime kilometers']
        self['lci_battery_cell_production_electricity'] = self['battery cell production electricity'] * self['battery cell mass'] *\
                                                      (1 + self['battery lifetime replacements' ]) / self['lifetime kilometers']

        self['lci_battery_cell_production_heat'] = 3.6 * self['battery cell production heat'] * self['battery cell mass']* \
                                                   (1 + self['battery lifetime replacements']) / self[
                                                       'lifetime kilometers']

        for pt in self.combustion_wo_cng:
            with self(pt) as cpm:
                cpm['lci_fuel_tank'] = cpm['fuel tank mass'] / cpm['lifetime kilometers']

        with self('ICEV-g') as cpm:
            cpm['lci_CNG_tank'] = cpm['fuel tank mass'] / cpm['lifetime kilometers']

        with self('FCEV') as cpm:
            cpm['lci_H2_tank'] = cpm['fuel tank mass'] / cpm['lifetime kilometers']

        # Energy chain

        for pt in self.electric:
            with self(pt) as cpm:
                cpm['lci_electricity'] = cpm['electricity consumption']

        for pt in self.petrol:
            with self(pt) as cpm:
                cpm['lci_petrol'] = cpm['fuel mass'] / cpm['range']

        with self('ICEV-d') as pt:
            pt['lci_diesel'] = pt['fuel mass'] / pt['range']

        with self('ICEV-g') as pt:
            pt['lci_CNG'] = pt['fuel mass'] / pt['range']

        with self('FCEV') as pt:
            pt['lci_h2'] = pt['fuel mass'] / pt['range']

        self['lci_tyre_wear'] = self['driving mass'] * -1 * 6.7568E-05 / 1180
        self['lci_brake_wear'] = self['driving mass'] * -1 * 1.0504E-06 / 1180
        self['lci_road_wear'] = self['driving mass'] * -1 * 1.1554E-05 / 1180

        self['lci_road'] = 5.37E-7 * self['driving mass']

        self['lci_CO2'] = (self['CO2 per kg fuel'] * self['fuel mass'])/ self['range']
        self['lci_SO2'] = (self['SO2 per kg fuel'] * self['fuel mass']) / self['range']
        self['lci_benzene'] = self['Benzene']
        self['lci_CH4'] = self['CH4']
        self['lci_CO'] = self['CO']
        self['lci_HC'] = self['HC']
        self['lci_N2O'] = self['N2O'] # combustion minus gas
        self['lci_NH3'] = self['NH3'] # combustion
        self['lci_NMVOC'] = self['NMVOC'] # combustion
        self['lci_NO2'] = self['NO2'] # combustion minus gas
        self['lci_NOx'] = self['NOx'] # combustion
        self['lci_PM'] = self['PM']

    def write_lci_excel_to_bw(self, db_name, filepath=None, objs=None, sections=None):
        """Export database `database_name` to an Excel spreadsheet.
        If a filepath is not specified, the inventory file is exported where the module resides.
        Taken from bw2io.export (https://bitbucket.org/cmutel/brightway2-io/src/default/)

        Returns the location of the exported inventory file.

        :param: db_name: Name of the database to be created.
        :type db_name: str
        :return: The file path of the exported file.
        :rtype: str

        """

        i = self.fill_in_datasets(db_name)

        data = []

        data.extend((['Database', db_name], ('format', 'Excel spreadsheet')))
        data.append([])

        for k in i.data:
            data.extend((['Activity', k['name']], ('code', k['code']),
                         ('location', k['location']),
                         ('production amount', float(k['production amount'])),
                         ('reference product', k.get('reference product')),
                         ('type', 'process'),
                         ('unit', k['unit']), ('worksheet name', 'None'), ['Exchanges'],
                         ['name', 'amount', 'database', 'location', 'unit', 'categories', 'type', 'reference product']
                         ))

            for e in k['exchanges']:
                data.append([e['name'], float(e['amount']), e['database'], e.get('location', 'None'), e['unit'],
                             '::'.join(e.get('categories', ())), e['type'], e.get('reference product')])
            data.append([])


        safe_name = safe_filename(db_name, False)

        parent = Path(getframeinfo(currentframe()).filename).resolve().parent

        if filepath is None:
            filepath = parent.joinpath('lci-' + safe_name + ".xlsx")
        else:
            filepath = filepath + '\lci-' + safe_name + ".xlsx"



        workbook = xlsxwriter.Workbook(filepath)
        bold = workbook.add_format({'bold': True})
        bold.set_font_size(12)
        highlighted = {'Activity', 'Database', 'Exchanges', 'Parameters', 'Database parameters', 'Project parameters'}
        frmt = lambda x: bold if row[0] in highlighted else None

        sheet = workbook.add_worksheet(create_valid_worksheet_name(db_name))

        for row_index, row in enumerate(data):
            for col_index, value in enumerate(row):
                if value is None:
                    continue
                elif isinstance(value, float):
                    sheet.write_number(row_index, col_index, value, frmt(value))
                else:
                    sheet.write_string(row_index, col_index, value, frmt(value))
        print('Inventories exported.')
        workbook.close()

        return filepath



    def write_lci_to_csv_simapro(self, db_name):
        """Export database `database_name` as a CSV file to be imported as a new project in SimaPro.
        Returns the file path of the exported CSV file.

        :param db_name: Name of the database to be created.
        :type db_name: str
        :return: The file path of the exported file.
        :rtype: str

        """



        return i

    def import_aux_datasets(self):
        """
        This method imports auxiliary inventory dataset of battery production, etc. into a new Brightway
        database.
        Returns an object from the `bw2io.ExcelImporter` class to which foreground inventories will be added.

        :return: An bw2io.ExcelImporter object.
        :rtype: bwio.ExcelImporter

        """
        parent = Path(getframeinfo(currentframe()).filename).resolve().parent
        filename = parent.joinpath('data/Additional datasets.xlsx')

        i = ExcelImporter(
            filename)
        return i

    def write_lci_to_bw(self, db_name):
        """
        This method first imports `additional inventories` (e.g., battery production, hydrogen tank manufacture, etc.)
        from an spreadsheet into a dictionary using one of Brightway's import functions.
        Then it adds the car inventories to it and returns an object from the `bw2io.ExcelImporter` class.
        :param db_name: Name of the database to be created.
        :type db_name: str
        :return: An bw2io.ExcelImporter object.
        :rtype: bwio.ExcelImporter
        """

        parent = Path(getframeinfo(currentframe()).filename).resolve().parent
        filename = parent.joinpath('data/dict_exchanges.csv')

        with open(filename) as f:
            csv_list = [[val.strip() for val in r.split(";")] for r in f.readlines()]

        (_, *header), *data = csv_list
        csv_dict = {}
        for row in data:
            key, *values = row
            csv_dict[key] = {key: value for key, value in zip(header, values)}

        list_act = []

        for pt in self.array.coords['powertrain'].values:
            for s in self.array.coords['size'].values:
                for y in self.array.coords['year'].values:

                    list_exc = []
                    # We insert first the reference product
                    list_exc.append(
                        {
                            'name': 'Passenger car, '+pt+", "+s+", "+str(y),
                            'database': db_name,
                            'amount': 1.0,
                            'unit': 'vehicle-kilometer',
                            'type': 'production',
                            'location': 'GLO'
                        }
                    )

                    for k in list(csv_dict.keys()):
                        value = self.array.sel(powertrain=pt, size=s, year=y, parameter=k).values[0]

                        if np.isinf(value):
                            print(pt, s, y, k, value)

                        if abs(value) != 0.0:

                            list_exc.append(
                                {
                                    'name': csv_dict[k]['Dataset name'],
                                    'database': csv_dict[k]['Database'],
                                    'amount': self.array.sel(powertrain=pt, size=s, year=y, parameter=k).values[0],
                                    'unit': csv_dict[k]['Unit'],
                                    'type': csv_dict[k]['Type'],
                                    'location': csv_dict[k].get('Location', 'None'),
                                    'reference product': csv_dict[k].get('reference product'),
                                    'categories': csv_dict[k].get('categories')
                                }
                            )

                    list_act.append({
                        'production amount':1,
                        'code': str(uuid.uuid1()),
                        'database': db_name,
                        'name': 'Passenger car, '+pt+", "+s+", "+str(y),
                        'unit': 'vehicle-kilometer',
                        'location': 'GLO',
                        'exchanges': list_exc,
                        'reference product': 'Passenger car, '+pt+", "+s+", "+str(y)
                    })

        i = self.import_aux_datasets()
        i.data.extend(list_act)
        i.db_name = db_name

        for k in i.data:

            k['database'] = db_name
            for e in k['exchanges']:
                if e['database'] == 'Additional datasets':
                    e['database'] = db_name

        i.apply_strategies()

        return i