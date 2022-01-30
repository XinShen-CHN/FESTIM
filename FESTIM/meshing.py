import fenics as f
import numpy as np
import FESTIM


class Mesh:
    """
    Mesh class

    Attributes:
        mesh (fenics.Mesh): the mesh
        volume_markers (fenics.MeshFunction): markers of the mesh cells
        surface_markers (fenics.MeshFunction): markers of the mesh facets
        dx (fenics.Measure):
        ds (fenics.Measure):
    """
    def __init__(self, mesh=None, volume_markers=None, surface_markers=None) -> None:
        """Inits Mesh

        Args:
            mesh (fenics.Mesh, optional): the mesh. Defaults to None.
            volume_markers (fenics.MeshFunction, optional): markers of the mesh cells. Defaults to None.
            surface_markers (fenics.MeshFunction, optional): markers of the mesh facets. Defaults to None.
        """
        self.mesh = mesh
        self.volume_markers = volume_markers
        self.surface_markers = surface_markers
        self.dx = None
        self.ds = None

    def define_measures(self):
        """Creates the fenics.Measure objects for self.dx and self.ds
        """

        self.ds = f.Measure(
            'ds', domain=self.mesh, subdomain_data=self.surface_markers)
        self.dx = f.Measure(
            'dx', domain=self.mesh, subdomain_data=self.volume_markers)


class Mesh1D(Mesh):
    """
    1D Mesh

    Attributes:
        size (float): the size of the 1D mesh

    """
    def __init__(self) -> None:
        super().__init__()
        self.size = None

    def define_markers(self, materials):
        """Iterates through the mesh and mark them
        based on their position in the domain

        Arguments:
            materials {FESTIM.Materials} -- contains the materials
        """
        mesh = self.mesh
        size = self.size
        volume_markers = f.MeshFunction("size_t", mesh, mesh.topology().dim(), 0)
        for cell in f.cells(mesh):
            for material in materials.materials:
                if len(materials.materials) == 1:
                    volume_markers[cell] = material.id
                else:
                    if cell.midpoint().x() >= material.borders[0] \
                    and cell.midpoint().x() <= material.borders[1]:
                        volume_markers[cell] = material.id
        surface_markers = f.MeshFunction(
            "size_t", mesh, mesh.topology().dim()-1, 0)
        surface_markers.set_all(0)
        i = 0
        for facet in f.facets(mesh):
            i += 1
            x0 = facet.midpoint()
            surface_markers[facet] = 0
            if f.near(x0.x(), 0):
                surface_markers[facet] = 1
            if f.near(x0.x(), size):
                surface_markers[facet] = 2
        self.volume_markers = volume_markers
        self.surface_markers = surface_markers

    def define_measures(self, materials):
        """Creates the fenics.Measure objects for self.dx and self.ds
        """
        if len(materials.materials) > 1:
            materials.check_borders(self.size)
        self.define_markers(materials)
        super().define_measures()


class MeshFromVertices(Mesh1D):
    """
    Description of MeshFromVertices

    Attributes:
        vertices (list): the mesh vertices
        size (type): the size of the 1D mesh
    """
    def __init__(self, vertices) -> None:
        """Inits MeshFromVertices

        Args:
            vertices (list): the mesh vertices
        """
        super().__init__()
        self.vertices = vertices
        self.size = max(vertices)
        self.generate_mesh_from_vertices()

    def generate_mesh_from_vertices(self):
        '''Generates a 1D mesh
        '''
        vertices = sorted(np.unique(self.vertices))
        nb_points = len(vertices)
        nb_cells = nb_points - 1
        editor = f.MeshEditor()
        mesh = f.Mesh()
        editor.open(mesh, "interval", 1, 1)  # top. and geom. dimension are both 1
        editor.init_vertices(nb_points)  # number of vertices
        editor.init_cells(nb_cells)     # number of cells
        for i in range(0, nb_points):
            editor.add_vertex(i, np.array([vertices[i]]))
        for j in range(0, nb_cells):
            editor.add_cell(j, np.array([j, j+1]))
        editor.close()
        self.mesh = mesh


class MeshFromRefinements(Mesh1D):
    """1D mesh with iterative refinements (on the left hand side of the domain)

    Attributes:
        initial_number_of_cells (int): initial number of cells before
            refinement
        size (float): total size of the 1D mesh
        refinements (list): list of refinements
    """
    def __init__(self, initial_number_of_cells, size, refinements=[]) -> None:
        """Inits MeshFromRefinements

        Args:
            initial_number_of_cells (float): initial number of cells before
            refinement
            size (float): total size of the 1D mesh
            refinements (list, optional): list of dicts
                {"x": ..., "cells": ...}. For each refinement, the mesh will
                have at least ["cells"] in [0, "x"]. Defaults to [].
        """
        super().__init__()
        self.initial_number_of_cells = initial_number_of_cells
        self.size = size
        self.refinements = refinements
        self.mesh_and_refine()

    def mesh_and_refine(self):
        """Mesh and refine iteratively until meeting the refinement
        conditions.
        """

        print('Meshing ...')
        initial_number_of_cells = self.initial_number_of_cells
        size = self.size
        mesh = f.IntervalMesh(initial_number_of_cells, 0, size)
        for refinement in self.refinements:
            nb_cells_ref = refinement["cells"]
            refinement_point = refinement["x"]
            print("Mesh size before local refinement is " +
                  str(len(mesh.cells())))
            coarse_mesh = True
            while len(mesh.cells()) < \
                    initial_number_of_cells + nb_cells_ref:
                cell_markers = f.MeshFunction(
                    "bool", mesh, mesh.topology().dim())
                cell_markers.set_all(False)
                for cell in f.cells(mesh):
                    if cell.midpoint().x() < refinement_point:
                        cell_markers[cell] = True
                        coarse_mesh = False
                mesh = f.refine(mesh, cell_markers)
                if coarse_mesh:
                    msg = "Infinite loop: Initial number " + \
                        "of cells might be too small"
                    raise ValueError(msg)
            print("Mesh size after local refinement is " +
                  str(len(mesh.cells())))
            initial_number_of_cells = len(mesh.cells())
        self.mesh = mesh


class MeshFromXDMF(Mesh):
    """
    Mesh read from XDMF files

    Attributes:
        volume_file (str): name of the volume file
        boundary_file (str): name of the boundary file
        mesh (fenics.Mesh): the mesh
    """
    def __init__(self, volume_file, boundary_file) -> None:
        """Inits MeshFromXDMF

        Args:
            volume_file (str): path to the volume file
            boundary_file (str): path the boundary file
        """
        super().__init__()

        self.volume_file = volume_file
        self.boundary_file = boundary_file

        self.mesh = f.Mesh()
        f.XDMFFile(self.volume_file).read(self.mesh)

        self.define_markers()

    def define_markers(self):
        """Reads volume and surface entities from XDMF files
        """
        mesh = self.mesh

        # Read tags for volume elements
        volume_markers = f.MeshFunction("size_t", mesh, mesh.topology().dim())
        f.XDMFFile(self.volume_file).read(volume_markers)

        # Read tags for surface elements
        # (can also be used for applying DirichletBC)
        surface_markers = \
            f.MeshValueCollection("size_t", mesh, mesh.topology().dim() - 1)
        f.XDMFFile(self.boundary_file).read(surface_markers, "f")
        surface_markers = f.MeshFunction("size_t", mesh, surface_markers)

        print("Succesfully load mesh with " + str(len(volume_markers)) + ' cells')
        self.volume_markers = volume_markers
        self.surface_markers = surface_markers
