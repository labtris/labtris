"""Reading boot parameters out of a vrnetlab launch.py.

The fixtures are reduced from the real files in srl-labs/vrnetlab, keeping
the shapes that matter: a platform that overrides one thing, one that
computes its values at runtime, and one that is two virtual machines.
"""

from labtris_api.vrnetlab import VRNETLAB_DEFAULTS, parse_launch

# cisco/csr1000v: overrides the NIC model and nothing else, and reads its
# NIC count from an argument rather than stating it.
CSR = '''
import vrnetlab

class CSR_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, nics, conn_mode):
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
        super(CSR_vm, self).__init__(username, password, disk_image=disk_image)
        self.num_nics = nics
        self.nic_type = "vmxnet3"
'''

# nokia/sros: RAM comes from the chassis definition at runtime; driveif is
# a literal.
SROS = '''
import vrnetlab

class SROS_vm(vrnetlab.VM):
    def __init__(self, username, password, ram, conn_mode):
        super().__init__(username, password, ram=ram, driveif="virtio")
        self.nic_type = "virtio-net-pci"
'''

# juniper/vmx: a control-plane VM and a forwarding VM, with different RAM
# and NIC counts.
VMX = '''
import vrnetlab

class VMX_vcp(vrnetlab.VM):
    def __init__(self, username, password, image):
        super(VMX_vcp, self).__init__(username, password, disk_image=image, ram=2048)
        self.num_nics = 0

class VMX_vfpc(vrnetlab.VM):
    def __init__(self):
        super().__init__(None, None, disk_image="/vmx/vfpc.img", cpu="SandyBridge", smp="3")
        self.num_nics = 96
        self.nic_type = "virtio-net-pci"
'''


def test_reads_only_what_the_file_states():
    found, classes = parse_launch(CSR)
    assert found == {"nic_type": "vmxnet3"}
    assert classes == ["CSR_vm"]
    # num_nics is `self.num_nics = nics` — an argument, not a literal. A
    # guess here would silently give the node the wrong interface count.
    assert "num_nics" not in found


def test_keyword_arguments_to_super():
    found, _ = parse_launch(SROS)
    assert found["driveif"] == "virtio"
    assert found["nic_type"] == "virtio-net-pci"
    # ram=ram is a name, not a literal, so it must fall through to the default.
    assert "ram" not in found


def test_multiple_vm_classes_are_reported():
    found, classes = parse_launch(VMX)
    assert classes == ["VMX_vcp", "VMX_vfpc"]
    # First occurrence wins, so RAM is the control plane's.
    assert found["ram"] == 2048
    assert found["num_nics"] == 0


def test_defaults_cover_every_key_the_importer_needs():
    for key in ("ram", "smp", "cpu", "driveif", "nic_type", "num_nics", "arch"):
        assert key in VRNETLAB_DEFAULTS


def test_unparseable_file_raises_rather_than_inventing():
    import pytest

    with pytest.raises(SyntaxError):
        parse_launch("class Broken(:\n")


def test_regex_escapes_in_a_real_file_do_not_warn():
    """Real launch.py files contain "\\.qcow2$" rather than a raw string.

    Python emits a SyntaxWarning compiling those. It is not the user's file
    and not their problem, so it must not reach their terminal.
    """
    import warnings as w

    with w.catch_warnings():
        w.simplefilter("error", SyntaxWarning)
        parse_launch('import re\nx = re.search("\\.license$", e)\n')
