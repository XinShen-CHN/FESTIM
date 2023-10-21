from dolfinx import fem
from dolfinx.nls.petsc import NewtonSolver
from dolfinx.io import XDMFFile
import basix
import ufl
from mpi4py import MPI
from dolfinx.fem import Function, form, assemble_scalar
from dolfinx.mesh import meshtags
from ufl import TestFunction, dot, grad, Measure, FacetNormal
import numpy as np
import tqdm.autonotebook


import festim as F


class HydrogenTransportProblem:
    """
    Hydrogen Transport Problem.

    Args:
        mesh (festim.Mesh): the mesh of the model
        subdomains (list of festim.Subdomain): the subdomains of the model
        species (list of festim.Species): the species of the model
        temperature (float or fem.Constant): the temperature of the model
        sources (list of festim.Source): the hydrogen sources of the model
        boundary_conditions (list of festim.BoundaryCondition): the boundary
            conditions of the model
        solver_parameters (dict): the solver parameters of the model
        exports (list of festim.Export): the exports of the model

    Attributes:
        mesh (festim.Mesh): the mesh of the model
        subdomains (list of festim.Subdomain): the subdomains of the model
        species (list of festim.Species): the species of the model
        temperature (fem.Constant): the temperature of the model
        boundary_conditions (list of festim.BoundaryCondition): the boundary
            conditions of the model
        solver_parameters (dict): the solver parameters of the model
        exports (list of festim.Export): the exports of the model
        dx (dolfinx.fem.dx): the volume measure of the model
        ds (dolfinx.fem.ds): the surface measure of the model
        function_space (dolfinx.fem.FunctionSpace): the function space of the
            model
        facet_meshtags (dolfinx.mesh.MeshTags): the facet tags of the model
        volume_meshtags (dolfinx.mesh.MeshTags): the volume tags of the
            model
        formulation (ufl.form.Form): the formulation of the model
        solver (dolfinx.nls.newton.NewtonSolver): the solver of the model

    Usage:
        >>> import festim as F
        >>> my_model = F.HydrogenTransportProblem()
        >>> my_model.mesh = F.Mesh(...)
        >>> my_model.subdomains = [F.Subdomain(...)]
        >>> my_model.species = [F.Species(name="H"), F.Species(name="Trap")]
        >>> my_model.temperature = 500
        >>> my_model.sources = [F.Source(...)]
        >>> my_model.boundary_conditions = [F.BoundaryCondition(...)]
        >>> my_model.initialise()

        or

        >>> my_model = F.HydrogenTransportProblem(
        ...     mesh=F.Mesh(...),
        ...     subdomains=[F.Subdomain(...)],
        ...     species=[F.Species(name="H"), F.Species(name="Trap")],
        ... )
        >>> my_model.initialise()

    """

    def __init__(
        self,
        mesh=None,
        subdomains=[],
        species=[],
        temperature=None,
        sources=[],
        boundary_conditions=[],
        settings=None,
        exports=[],
    ) -> None:
        self.mesh = mesh
        self.subdomains = subdomains
        self.species = species
        self.temperature = temperature
        self.sources = sources
        self.boundary_conditions = boundary_conditions
        self.settings = settings
        self.exports = exports

        self.dx = None
        self.ds = None
        self.function_space = None
        self.facet_meshtags = None
        self.volume_meshtags = None
        self.formulation = None
        self.volume_subdomains = []
        self.bc_forms = []

    @property
    def temperature(self):
        return self._temperature

    @temperature.setter
    def temperature(self, value):
        if value is None:
            self._temperature = value
        else:
            self._temperature = F.as_fenics_constant(value, self.mesh.mesh)

    def initialise(self):
        self.define_function_space()
        self.define_markers_and_measures()
        self.assign_functions_to_species()

        self.t = fem.Constant(self.mesh.mesh, 0.0)
        self.dt = self.settings.stepsize.get_dt(self.mesh.mesh)

        self.define_boundary_conditions()
        self.create_formulation()
        self.create_solver()
        self.defing_export_writers()

    def defing_export_writers(self):
        """Defines the export writers of the model"""
        for export in self.exports:
            # TODO implement when export.field is an int or str
            # then find solution from index of species

            if isinstance(export, (F.VTXExport, F.XDMFExport)):
                export.define_writer(MPI.COMM_WORLD)
                if isinstance(export, F.XDMFExport):
                    export.writer.write_mesh(self.mesh.mesh)

    def define_function_space(self):
        element_CG = basix.ufl.element(
            basix.ElementFamily.P,
            self.mesh.mesh.basix_cell(),
            1,
            basix.LagrangeVariant.equispaced,
        )
        elements = []
        if len(self.species) <= 1:
            mixed_element = element_CG
        else:
            for spe in self.species:
                if isinstance(spe, F.Species):
                    # TODO check if mobile or immobile for traps
                    elements.append(element_CG)
            mixed_element = ufl.MixedElement(elements)

        self.function_space = fem.FunctionSpace(self.mesh.mesh, mixed_element)

        self.u = Function(self.function_space)
        self.u_n = Function(self.function_space)

    def assign_functions_to_species(self):
        """Creates for each species the solution, prev solution and test
        function"""

        if len(self.species) == 1:
            sub_solutions = [self.u]
            sub_prev_solution = [self.u_n]
            sub_test_functions = [ufl.TestFunction(self.function_space)]
            self.species[0].sub_function_space = self.function_space
            self.species[0].post_processing_solution = fem.Function(
                self.species[0].sub_function_space
            )
        else:
            sub_solutions = list(ufl.split(self.u))
            sub_prev_solution = list(ufl.split(self.u_n))
            sub_test_functions = list(ufl.TestFunctions(self.function_space))
            for idx, spe in enumerate(self.species):
                spe.sub_function_space = self.function_space.sub(idx)
                post_processing_function_space, _ = spe.sub_function_space.collapse()
                spe.post_processing_solution = fem.Function(
                    post_processing_function_space
                )

        for idx, spe in enumerate(self.species):
            spe.solution = sub_solutions[idx]
            spe.post_processing_solution = sub_solutions[idx]
            spe.prev_solution = sub_prev_solution[idx]
            spe.test_function = sub_test_functions[idx]

    def define_markers_and_measures(self):
        """Defines the markers and measures of the model"""

        dofs_facets, tags_facets = [], []

        # find all cells in domain and mark them as 0
        num_cells = self.mesh.mesh.topology.index_map(self.mesh.vdim).size_local
        mesh_cell_indices = np.arange(num_cells, dtype=np.int32)
        tags_volumes = np.full(num_cells, 0, dtype=np.int32)

        for sub_dom in self.subdomains:
            if isinstance(sub_dom, F.SurfaceSubdomain1D):
                dof = sub_dom.locate_dof(self.mesh.mesh, self.mesh.fdim)
                dofs_facets.append(dof)
                tags_facets.append(sub_dom.id)
            if isinstance(sub_dom, F.VolumeSubdomain1D):
                # find all cells in subdomain and mark them as sub_dom.id
                self.volume_subdomains.append(sub_dom)
                entities = sub_dom.locate_subdomain_entities(
                    self.mesh.mesh, self.mesh.vdim
                )
                tags_volumes[entities] = sub_dom.id

        # check if all borders are defined
        if isinstance(self.mesh, F.Mesh1D):
            self.mesh.check_borders(self.volume_subdomains)

        # dofs and tags need to be in np.in32 format for meshtags
        dofs_facets = np.array(dofs_facets, dtype=np.int32)
        tags_facets = np.array(tags_facets, dtype=np.int32)

        # define mesh tags
        self.facet_meshtags = meshtags(
            self.mesh.mesh, self.mesh.fdim, dofs_facets, tags_facets
        )
        self.volume_meshtags = meshtags(
            self.mesh.mesh, self.mesh.vdim, mesh_cell_indices, tags_volumes
        )

        # define measures
        self.ds = Measure(
            "ds", domain=self.mesh.mesh, subdomain_data=self.facet_meshtags
        )
        self.dx = Measure(
            "dx", domain=self.mesh.mesh, subdomain_data=self.volume_meshtags
        )

    def define_boundary_conditions(self):
        """Defines the dirichlet boundary conditions of the model"""
        for bc in self.boundary_conditions:
            if isinstance(bc.species, str):
                # if name of species is given then replace with species object
                name = bc.species
                bc.species = F.find_species_from_name(name, self.species)
            if isinstance(bc, F.DirichletBC):
                bc_dofs = bc.define_surface_subdomain_dofs(
                    self.facet_meshtags, self.mesh, bc.species.sub_function_space
                )
                bc.create_value(
                    self.mesh.mesh,
                    bc.species.sub_function_space,
                    self.temperature,
                    self.t,
                )
                form = bc.create_formulation(
                    dofs=bc_dofs, function_space=bc.species.sub_function_space
                )
                self.bc_forms.append(form)

    def create_formulation(self):
        """Creates the formulation of the model"""
        if len(self.sources) > 1:
            raise NotImplementedError("Sources not implemented yet")

        self.formulation = 0

        for spe in self.species:
            u = spe.solution
            u_n = spe.prev_solution
            v = spe.test_function

            for vol in self.volume_subdomains:
                D = vol.material.get_diffusion_coefficient(
                    self.mesh.mesh, self.temperature, spe, self.species
                )

                self.formulation += dot(D * grad(u), grad(v)) * self.dx(vol.id)
                self.formulation += ((u - u_n) / self.dt) * v * self.dx(vol.id)

                # add sources
                # TODO implement this
                # for source in self.sources:
                #     # f = Constant(my_mesh.mesh, (PETSc.ScalarType(0)))
                #     if source.species == spe:
                #         formulation += source * v * self.dx
                # add fluxes
                # TODO implement this
                # for bc in self.boundary_conditions:
                #     pass
                #     if bc.species == spe and bc.type != "dirichlet":
                #         formulation += bc * v * self.ds

    def create_solver(self):
        """Creates the solver of the model"""
        problem = fem.petsc.NonlinearProblem(
            self.formulation,
            self.u,
            bcs=self.bc_forms,
        )
        self.solver = NewtonSolver(MPI.COMM_WORLD, problem)
        self.solver.atol = self.settings.atol
        self.solver.rtol = self.settings.rtol
        self.solver.max_it = self.settings.max_iterations

    def run(self):
        """Runs the model for a given time

        Returns:
            list of float: the times of the simulation
            list of float: the fluxes of the simulation
        """
        times, flux_values = [], []

        n = self.mesh.n
        D = self.subdomains[0].material.get_diffusion_coefficient(
            self.mesh.mesh, self.temperature, self.species[0], self.species
        )
        cm = self.species[0].solution
        progress = tqdm.autonotebook.tqdm(
            desc="Solving H transport problem",
            total=self.settings.final_time,
            unit_scale=True,
        )
        while self.t.value < self.settings.final_time:
            progress.update(self.dt.value)
            self.t.value += self.dt.value

            # update boundary conditions
            for bc in self.boundary_conditions:
                bc.update(float(self.t))

            self.solver.solve(self.u)

            if len(self.species) == 1:
                cm = self.u
                self.species[0].post_processing_solution = self.u
                # post processing
                surface_flux = form(D * dot(grad(cm), n) * self.ds(2))
                flux = assemble_scalar(surface_flux)
                flux_values.append(flux)
                times.append(float(self.t))

            # TODO in multi-species export all functions
            # else:
            #     res = list(self.u.split())
            #     for idx, spe in enumerate(self.species):
            #         spe.solution = res[idx]

            for export in self.exports:
                if isinstance(export, (F.VTXExport, F.XDMFExport)):
                    export.write(float(self.t))

            # update previous solution
            self.u_n.x.array[:] = self.u.x.array[:]

        return times, flux_values
