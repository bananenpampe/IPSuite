"""Use packmole to create a periodic box"""

import logging
import pathlib
import re
import subprocess
import threading

import ase
import ase.units
import numpy as np
import zntrack
from ase.visualize import view

from ipsuite import base, fields
from ipsuite.utils.ase_sim import get_box_from_density

log = logging.getLogger(__name__)


def get_packmol_version():
    """
    Get the version of the local installed packmol.
    """

    # packmol when called with --version
    # will just print a standard output and wait for user input
    # this function is a bit akward as it needs to read the output
    # and terminate the subprocess without using a timeout

    try:
        process = subprocess.Popen(
            ["packmol"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        def read_output(process, output_list):
            try:
                for line in process.stdout:
                    output_list.append(line)
                    if "Version" in line:
                        break
            except Exception as e:
                output_list.append(f"Error: {str(e)}")

        output_lines = []
        reader_thread = threading.Thread(target=read_output, args=(process, output_lines))
        reader_thread.start()

        reader_thread.join(timeout=1)

        if process.poll() is None:
            process.terminate()
            process.wait()

        reader_thread.join()
        full_output = "".join(output_lines)

        version_match = re.search(r"Version (\d+\.\d+\.\d+)", full_output)

        if version_match:
            return version_match.group(1)
        else:
            raise ValueError(f"Could not find version in packmol output: {full_output}")

    except Exception as e:
        return f"An error occurred: {str(e)}"


class Packmol(base.IPSNode):
    """

    Attributes
    ----------
    data: list[list[ase.Atoms]]
        For each entry in the list the last ase.Atoms object is used to create the
        new structure.
    data_ids: list[int]
        The id of the data to use for each entry in data. If None the last entry.
        Has to be the same length as data. data: [[A], [B]], [-1, 3] -> [A[-1], B[3]]
    count: list[int]
        Number of molecules to add for each entry in data.
    tolerance : float
        Tolerance for the distance of atoms in angstrom.
    box : list[float]
        Box size in angstrom. Either density or box is required.
    density : float
        Density of the system in kg/m^3. Either density or box is required.
    pbc : bool
        If True the periodic boundary conditions are set for the generated structure and
        the box used by packmol is scaled by the tolerance, to avoid overlapping atoms
        with periodic boundary conditions.
    """

    data: list[list[ase.Atoms]] = zntrack.deps()
    data_ids: list[int] = zntrack.params(None)
    count: list = zntrack.params()
    tolerance: float = zntrack.params(2.0)
    box: list = zntrack.params(None)
    density: float = zntrack.params(None)
    structures = zntrack.outs_path(zntrack.nwd / "packmol")
    atoms = fields.Atoms()
    pbc: bool = zntrack.params(True)

    def _post_init_(self):
        if self.box is None and self.density is None:
            raise ValueError("Either box or density must be set.")
        if len(self.data) != len(self.count):
            raise ValueError("The number of data and count must be the same.")
        if self.data_ids is not None and len(self.data) != len(self.data_ids):
            raise ValueError("The number of data and data_ids must be the same.")
        if self.box is not None and isinstance(self.box, (int, float)):
            self.box = [self.box, self.box, self.box]

    def run(self):
        self.structures.mkdir(exist_ok=True, parents=True)
        for idx, atoms in enumerate(self.data):
            atoms = atoms[-1] if self.data_ids is None else atoms[self.data_ids[idx]]
            ase.io.write(self.structures / f"{idx}.xyz", atoms)

        if self.density is not None:
            self._get_box_from_molar_volume()

        file = f"""
        tolerance {self.tolerance}
        filetype xyz
        output mixture.xyz
        """

        packmol_version = get_packmol_version()
        log.info(f"Packmol version: {packmol_version}")

        packmol_version = int(packmol_version.replace(".", ""))

        if self.pbc and packmol_version >= 20150:
            scaled_box = self.box

            request_pbc_str = f"""
            pbc {" ".join([f"{x:.4f}" for x in scaled_box])}
            """

            file += request_pbc_str

        elif self.pbc and packmol_version < 20150:
            scaled_box = [x - 2 * self.tolerance for x in self.box]
            log.warning(
                "Packmol version is too old to use periodic boundary conditions.         "
                "       The box size will be scaled by tolerance to avoid overlapping"
                " atoms."
            )
        else:
            scaled_box = self.box

        for idx, count in enumerate(self.count):
            file += f"""
            structure {idx}.xyz
                number {count}
                inside box 0 0 0 {" ".join([f"{x:.4f}" for x in scaled_box])}
            end structure
            """
        with pathlib.Path(self.structures / "packmole.inp").open("w") as f:
            f.write(file)

        subprocess.check_call("packmol < packmole.inp", shell=True, cwd=self.structures)

        atoms = ase.io.read(self.structures / "mixture.xyz")
        if self.pbc:
            atoms.cell = self.box
            atoms.pbc = True
        self.atoms = [atoms]

    def _get_box_from_molar_volume(self):
        """Get the box size from the molar volume"""
        self.box = get_box_from_density(self.data, self.count, self.density)
        log.info(f"estimated box size: {self.box}")

    def view(self) -> view:
        return view(self.atoms, viewer="x3d")


class MultiPackmol(Packmol):
    """Create multiple configurations with packmol.

    This Node generates multiple configurations with packmol.
    This is best used in conjunction with SmilesToConformers:

    Example
    -------
    .. testsetup::
        >>> tmp_path = utils.docs.create_dvc_git_env_for_doctest()

    >>> import ipsuite as ips
    >>> with ips.Project(automatic_node_names=True) as project:
    ...     water = ips.configuration_generation.SmilesToConformers(
    ...         smiles='O', numConfs=100
    ...         )
    ...     boxes = ips.configuration_generation.MultiPackmol(
    ...         data=[water.atoms], count=[10], density=997, n_configurations=10
    ...         )
    >>> project.run()

    .. testcleanup::
        >>> tmp_path.cleanup()

    Attributes
    ----------
    n_configurations : int
        Number of configurations to create.
    seed : int
        Seed for the random number generator.
    """

    n_configurations: int = zntrack.params()
    seed: int = zntrack.params(42)
    data_ids = None

    def run(self):
        np.random.seed(self.seed)
        self.atoms = []

        if self.density is not None:
            self._get_box_from_molar_volume()

        file_head = f"""
        tolerance {self.tolerance}
        filetype xyz
        """

        packmol_version = get_packmol_version()
        log.info(f"Packmol version: {packmol_version}")

        packmol_version = int(packmol_version.replace(".", ""))

        if self.pbc and packmol_version >= 20150:
            scaled_box = self.box

            request_pbc_str = f"""
            pbc {" ".join([f"{x:.4f}" for x in scaled_box])}
            """

            file_head += request_pbc_str

        elif self.pbc and packmol_version < 20150:
            scaled_box = [x - 2 * self.tolerance for x in self.box]
            log.warning(
                "Packmol version is too old to use periodic boundary conditions.         "
                "       The box size will be scaled by tolerance to avoid overlapping"
                " atoms."
            )
        else:
            scaled_box = self.box

        self.structures.mkdir(exist_ok=True, parents=True)
        for idx, atoms_list in enumerate(self.data):
            for jdx, atoms in enumerate(atoms_list):
                ase.io.write(self.structures / f"{idx}_{jdx}.xyz", atoms)

        for idx in range(self.n_configurations):
            file = (
                file_head
                + f"""
            output mixture_{idx}.xyz
            """
            )
            for jdx, count in enumerate(self.count):
                choices = np.random.choice(len(self.data[jdx]), count)
                for kdx in choices:
                    file += f"""
                    structure {jdx}_{kdx}.xyz
                        number 1
                        inside box 0 0 0 {" ".join([f"{x:.4f}" for x in scaled_box])}
                    end structure
                    """
            with pathlib.Path(self.structures / f"packmole_{idx}.inp").open("w") as f:
                f.write(file)

            subprocess.check_call(
                f"packmol < packmole_{idx}.inp", shell=True, cwd=self.structures
            )

            atoms = ase.io.read(self.structures / f"mixture_{idx}.xyz")
            if self.pbc:
                atoms.cell = self.box
                atoms.pbc = True

            self.atoms.append(atoms)
