"""The installer's cross-file references, checked without building an ISO.

Nothing here builds anything. The failures it catches are the ones that cost
the most to find the other way: a path in the autoinstall answer file that no
longer exists, a systemd unit the install script installs but nobody ships, a
Plymouth theme pointing at a script filename that was renamed. Every one of
those produces an ISO that builds cleanly and then fails on the machine you
were installing, an hour later, with no useful message.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ISO = ROOT / "packaging" / "iso"
AUTOINSTALL = ISO / "autoinstall" / "user-data"
INSTALL_SH = ROOT / "packaging" / "install-labtris.sh"


@pytest.fixture(scope="module")
def autoinstall() -> dict:
    return yaml.safe_load(AUTOINSTALL.read_text())["autoinstall"]


def test_the_answer_file_is_valid_yaml_and_declares_its_version() -> None:
    """Subiquity rejects the whole file on a parse error and falls back to a
    manual install — on the machine nobody is watching."""
    data = yaml.safe_load(AUTOINSTALL.read_text())

    assert data["autoinstall"]["version"] == 1


def test_nothing_is_left_interactive(autoinstall: dict) -> None:
    """A single un-answered section turns an unattended install back into one
    that waits for a keyboard, silently."""
    assert autoinstall["interactive-sections"] == []


def test_the_install_answers_everything_subiquity_would_ask(autoinstall: dict) -> None:
    for key in ("locale", "keyboard", "identity", "storage"):
        assert key in autoinstall, f"{key} unanswered — the installer will stop and ask"
    for key in ("hostname", "username", "password"):
        assert autoinstall["identity"].get(key)


def test_the_first_boot_password_is_forced_to_change(autoinstall: dict) -> None:
    """The ISO ships one known password, so it has to be aged on first login —
    but NOT from a late-command. subiquity hands account creation to
    cloud-init, which makes the user on the first boot; `passwd --expire`
    during the install therefore runs against a user that does not exist,
    fails, and takes the whole installation down on its last line."""
    commands = autoinstall["late-commands"]

    assert not any("passwd" in c for c in commands), (
        "password expiry cannot happen in a late-command — the account is not created yet"
    )
    assert any("labtris-expire-admin" in c for c in commands), (
        "nothing ages the shipped password, so every appliance keeps it"
    )


def test_the_expiry_unit_waits_for_the_account_to_exist() -> None:
    """Ordering is the whole point of moving it: cloud-init creates the user,
    so this has to run after cloud-init and not merely at boot."""
    unit = (
        ISO / "overlay" / "etc" / "systemd" / "system" / "labtris-expire-admin.service"
    ).read_text()

    assert "After=cloud-init.service" in unit
    assert "chage -d 0" in unit
    assert "|| true" in unit, "a failed expiry must not block the boot"


def test_every_path_the_late_commands_copy_from_is_actually_shipped(
    autoinstall: dict,
) -> None:
    """`cp /cdrom/<x>` for an <x> the build never puts there fails an hour into
    an install with 'No such file or directory' and no context."""
    # Derived from build.sh's own -map arguments rather than listed here.
    # A hardcoded set went stale twice in one afternoon, once for the bundled
    # packages and once for the wheels — each time reporting a failure that was
    # the test's fault, not the build's.
    build = (ISO / "build.sh").read_text()
    shipped = set(re.findall(r'-map "\$WORK/add/[^"]+"\s+/([A-Za-z0-9_.-]+)', build))
    assert shipped, "no -map arguments found; the build no longer looks like this"
    for command in autoinstall["late-commands"]:
        for ref in re.findall(r"/cdrom/([A-Za-z0-9_-]+)", command):
            assert ref in shipped, f"late-command reads /cdrom/{ref}, which build.sh never creates"


def test_the_late_commands_run_the_install_script_that_exists(autoinstall: dict) -> None:
    joined = " ".join(autoinstall["late-commands"])
    referenced = re.search(r"(/opt/labtris-src/packaging/\S+\.sh)", joined)

    assert referenced, "the answer file never runs an install script"
    relative = referenced.group(1).replace("/opt/labtris-src/", "")
    assert (ROOT / relative).exists(), f"{relative} is referenced but not in the repository"


def test_the_scripts_are_executable() -> None:
    """A file copied without its mode bit is a late-command that fails with
    'Permission denied' after the disk has already been repartitioned."""
    for script in (
        INSTALL_SH,
        ISO / "build.sh",
        ISO / "test-boot.sh",
        ISO / "overlay" / "etc" / "update-motd.d" / "00-labtris",
    ):
        assert script.exists(), f"{script} is missing"
        assert script.stat().st_mode & stat.S_IXUSR, f"{script} is not executable"


def test_the_install_script_only_installs_units_that_exist() -> None:
    """It copies unit files into /etc/systemd/system by name; a rename here
    that misses there produces a host with no service and a clean exit code."""
    text = INSTALL_SH.read_text()

    for unit in re.findall(r"packaging/systemd/(\S+\.service)", text):
        assert (ROOT / "packaging" / "systemd" / unit).exists(), f"{unit} is not shipped"


def test_the_install_script_only_installs_configs_that_exist() -> None:
    text = INSTALL_SH.read_text()

    for conf in re.findall(r'"\$PREFIX/(packaging/\S+\.conf)"', text):
        assert (ROOT / conf).exists(), f"{conf} is referenced but not in the repository"


def test_the_database_password_is_generated_never_shipped() -> None:
    """An appliance image with a known database password is how a whole
    product line ends up sharing one credential."""
    text = INSTALL_SH.read_text()

    assert "/dev/urandom" in text
    # The development placeholder must not leak into the installed config.
    assert "pnl:pnl" not in text


def test_the_env_file_pins_the_database_url() -> None:
    """The bug this prevents: the URL living only in the shell that started
    the service, so any restart comes up against the wrong Postgres."""
    text = INSTALL_SH.read_text()

    assert "LABTRIS_DATABASE_URL=" in text
    assert "/etc/labtris" in text or "$CONFDIR" in text


def test_acceleration_defaults_to_software_emulation() -> None:
    """Nested virt that reports success and then hangs on the first vcpu is
    why kvm is opt-in rather than assumed."""
    text = INSTALL_SH.read_text()

    assert "LABTRIS_QEMU_ACCEL=tcg" in text


def test_the_plymouth_theme_points_at_files_it_ships() -> None:
    theme_dir = ISO / "overlay" / "usr" / "share" / "plymouth" / "themes" / "labtris"
    theme = (theme_dir / "labtris.plymouth").read_text()

    for referenced in re.findall(r"ScriptFile=(\S+)", theme):
        assert (theme_dir / Path(referenced).name).exists(), f"{referenced} is missing"


def test_the_motd_resolves_a_real_address_rather_than_a_placeholder() -> None:
    """The console banner exists to answer "where do I browse to". A hardcoded
    example address would be worse than printing nothing."""
    motd = (ISO / "overlay" / "etc" / "update-motd.d" / "00-labtris").read_text()

    assert "hostname -I" in motd or "ip -4 route get" in motd
    assert "8081" in motd


def test_the_build_pins_a_release_rather_than_tracking_latest() -> None:
    """A build whose base image changes underneath it is not reproducible, and
    the change would first show up as an install failing on one machine."""
    build = (ISO / "build.sh").read_text()

    assert re.search(r'RELEASE=\$\{RELEASE:-\d+\.\d+', build)


def test_the_build_verifies_the_download() -> None:
    """A truncated ISO extracts far enough to look fine and yields a machine
    that will not boot."""
    build = (ISO / "build.sh").read_text()

    assert "SHA256SUMS" in build
    assert "checksum mismatch" in build


def test_no_guest_images_are_baked_into_the_iso() -> None:
    """Vendor images cannot be redistributed, and the free ones would add tens
    of gigabytes to save a fetch the QEMU backend already does on demand."""
    build = (ISO / "build.sh").read_text()
    excluded = re.search(r"tar -C \"\$ROOT\"(.*?)-cf -", build, re.S)

    assert excluded, "the source is not tarred into the ISO in the way this test expects"
    # Anchored form: --exclude=./x rather than --exclude=x, so the pattern
    # cannot also swallow a nested directory of the same name.
    for path in ("./.git", "./.venv", "./node_modules", "./.cache"):
        assert f"--exclude={path}" in excluded.group(1)


def test_the_installer_talks_to_both_a_screen_and_a_serial_port() -> None:
    """Without console=ttyS0 the installer goes silent the moment the kernel
    takes over from GRUB, which is when you most want to watch it — and a
    headless server reached over a serial console shows nothing at all.

    tty0 is listed first so the last entry wins as /dev/console: output still
    reaches a monitor if there is one."""
    build = (ISO / "build.sh").read_text()

    assert "console=tty0" in build
    assert "console=ttyS0" in build


def test_the_install_entries_are_not_quieted() -> None:
    """`quiet splash` on an installer hides the thing being installed. The
    installed system generates its own boot config, so this costs no splash
    on later boots."""
    build = (ISO / "build.sh").read_text()
    args = re.search(r"KERNEL_ARGS='([^']*)'", build)

    assert args, "the kernel arguments are no longer where this test looks"
    assert "quiet" not in args.group(1)


def test_the_boot_test_can_see_a_screen_it_cannot_hear() -> None:
    """The monitor has to be reachable independently: multiplexed onto stdio
    it is unusable once output is redirected to a log, and a screendump is the
    only way to inspect an installer that stopped writing to serial."""
    script = (ISO / "test-boot.sh").read_text()

    assert "-monitor" in script
    assert "unix:" in script


def test_a_rebuild_does_not_trip_over_the_previous_image() -> None:
    """xorriso refuses an output device that already holds data, so without
    this every build after the first fails on the artifact the first made."""
    build = (ISO / "build.sh").read_text()

    assert 'rm -f "$OUT"' in build


def test_the_kernel_line_carries_no_semicolon() -> None:
    """GRUB treats ; as a command separator. A ds=nocloud;s=... argument has to
    survive a sed replacement with its escape intact to reach grub.cfg, and
    when it did not the ISO booted perfectly and then stopped to ask for a
    language — the failure looks like success right up to the point nobody is
    there to answer."""
    build = (ISO / "build.sh").read_text()
    args = re.search(r"KERNEL_ARGS='([^']*)'", build)

    assert args, "the kernel arguments moved"
    assert ";" not in args.group(1)


def test_the_answer_file_ships_where_subiquity_looks_by_itself() -> None:
    """/autoinstall.yaml on the install media needs no ds= argument at all,
    which is what lets the kernel command line stay free of semicolons."""
    build = (ISO / "build.sh").read_text()

    assert "/autoinstall.yaml" in build


def test_nothing_that_needs_a_running_system_happens_before_the_split() -> None:
    """The bug this pins, found by booting the ISO: late-commands run under
    `curtin in-target`, which is a chroot. systemd is not PID 1 there, so the
    original `systemctl enable --now postgresql docker` failed the moment apt
    finished — leaving an install that reported packages installed and never
    created /opt/labtris.

    Every command needing a live machine must sit inside finalise()."""
    text = INSTALL_SH.read_text()
    body, _, deferred = text.partition("finalise() {")
    assert deferred, "finalise() has gone — the two-phase split is the whole point"

    # Comments stripped: the explanation of this very bug names the commands
    # it is warning about, and matching prose would fail on the fix's own
    # documentation.
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("systemctl enable --now", "systemctl start", "systemctl reload"):
        assert forbidden not in code, (
            f"{forbidden!r} runs before the split; it cannot work in a chroot"
        )


def test_the_database_is_only_touched_where_postgres_can_run() -> None:
    """createdb against a chroot has nothing listening to talk to."""
    text = INSTALL_SH.read_text()
    body, _, _deferred = text.partition("finalise() {")

    assert "createdb" not in body
    assert "alembic" not in body


def test_a_chroot_install_stages_a_first_boot_unit() -> None:
    """Without this the packages land and nothing ever starts, which is
    indistinguishable from a successful install until someone browses to it."""
    text = INSTALL_SH.read_text()

    assert "should_finalise_now" in text
    assert "labtris-firstboot.service" in text
    assert "--finalise" in text


def test_the_first_boot_unit_removes_itself() -> None:
    """It exists to run once. Left enabled it repeats a 900-second unit on
    every boot."""
    text = INSTALL_SH.read_text()

    assert "systemctl disable labtris-firstboot.service" in text


def test_the_first_boot_unit_logs_where_someone_will_look() -> None:
    """A silent failure on first boot is the same invisible failure again."""
    text = INSTALL_SH.read_text()

    assert "journal+console" in text


def test_postgres_readiness_is_waited_for_not_assumed() -> None:
    """`systemctl --now` returns when the unit is active, which on a first
    boot is still before the socket accepts connections."""
    text = INSTALL_SH.read_text()

    assert "postgres did not become ready" in text


def test_the_iso_tells_the_installer_it_is_staging() -> None:
    """Rather than making the script guess. The guess was wrong: curtin
    bind-mounts the installer's /run into the target, so /run/systemd/system
    exists and belongs to the installer. finalise() ran, enabled services into
    the target, and failed trying to start them on the wrong machine."""
    autoinstall = AUTOINSTALL.read_text()
    joined = " ".join(yaml.safe_load(autoinstall)["autoinstall"]["late-commands"])

    assert "--staged" in joined


def test_an_explicit_stage_flag_beats_any_environment_probe() -> None:
    """The caller knows; sniffing the environment is the fallback, not the
    primary signal."""
    text = INSTALL_SH.read_text()
    fn = text.partition("should_finalise_now() {")[2].partition("}")[0]

    # The flag is checked before anything is probed.
    assert fn.index("STAGED") < fn.index("systemd-detect-virt")


def test_the_chroot_probe_does_not_rely_on_run_systemd_alone() -> None:
    """That directory is bind-mounted in from the installer and says nothing
    about the filesystem being installed into."""
    text = INSTALL_SH.read_text()

    assert "systemd-detect-virt --chroot" in text


def test_branding_cannot_abort_the_install() -> None:
    """A missing logo.png broke the Plymouth theme, whose initramfs hook then
    failed, whose non-zero exit then aborted the entire installation. Every
    cosmetic step is guarded, because no splash screen is worth an install."""
    autoinstall = AUTOINSTALL.read_text()
    commands = yaml.safe_load(autoinstall)["autoinstall"]["late-commands"]

    for command in commands:
        if "update-initramfs" in command or "update-alternatives" in command:
            assert "|| true" in command, f"unguarded cosmetic step: {command[:70]}"


def test_a_theme_without_its_logo_is_not_shipped() -> None:
    """Image("logo.png") on a file that does not exist is a broken theme, not
    a degraded one. Dropping it loses branding; shipping it lost the install."""
    build = (ISO / "build.sh").read_text()
    fallback = build.partition("no rsvg-convert or convert")[2]

    assert "rm -rf" in fallback and "plymouth" in fallback


def test_nothing_is_written_into_the_theme_after_it_may_have_been_deleted() -> None:
    """The first attempt at the fix deleted the theme directory and then wrote
    bar.png into it anyway. The redirect failed, `set -e` killed the build, and
    it exited with no message at all — twice, before I looked at the ordering.

    So the remaining assets must sit inside a branch that runs only when the
    logo actually exists."""
    build = (ISO / "build.sh").read_text()
    guard = build.index('if [ -s "$PLYMOUTH/logo.png" ]')
    bar = build.index('base64 -d > "$PLYMOUTH/bar.png"')
    removal = build.index('rm -rf "$WORK/add/labtris-overlay/usr/share/plymouth"')

    assert guard < bar, "bar.png is written outside the exists-guard"
    assert bar < removal, "an asset is written after the directory may be gone"


def test_the_splash_is_decided_by_the_asset_not_the_tool() -> None:
    """A rasteriser that runs and silently produces nothing would otherwise
    leave the same broken theme the tool-presence check was meant to prevent."""
    build = (ISO / "build.sh").read_text()

    assert '[ -s "$PLYMOUTH/logo.png" ]' in build


def test_the_admin_password_is_a_real_crypt_hash() -> None:
    """An invented hash does not fail where you wrote it. Subiquity quietly
    skips creating the account, the install runs to its very last command, and
    dies expiring a password for a user that was never made.

    Structure only — SHA-512 crypt is $6$, a salt, and an 86-character
    digest. Anything shorter is a string somebody typed."""
    identity = yaml.safe_load(AUTOINSTALL.read_text())["autoinstall"]["identity"]
    parts = identity["password"].split("$")

    assert parts[1] == "6", "not SHA-512 crypt"
    assert 8 <= len(parts[2]) <= 16, f"implausible salt: {len(parts[2])} chars"
    assert len(parts[3]) == 86, f"digest is {len(parts[3])} chars, not 86"


def test_alembic_runs_from_the_install_directory() -> None:
    """alembic resolves script_location against the working directory, not
    against the file given to -c. Started by systemd with no WorkingDirectory
    the cwd is /, so `migrations` becomes `/migrations` and the upgrade fails
    with "Path doesn't exist" — which never happens in development, where it
    is always run from inside the checkout."""
    text = INSTALL_SH.read_text()
    schema = text.partition('say "Schema"')[2].partition("say ")[0]

    assert 'cd "$PREFIX"' in schema, "alembic must run from the install directory"


def test_the_vnc_password_does_not_depend_on_a_tool_that_may_be_absent() -> None:
    """It was set through the QEMU monitor with socat. socat was not installed,
    the step was skipped silently, and QEMU ran with password=on and no
    password — refusing every connection with no clue why. The secret object
    is part of QEMU itself."""
    script = (ISO / "test-boot.sh").read_text()

    assert "password-secret=vncsec" in script
    assert "set_password vnc" not in script, "back to the monitor-and-socat approach"


def test_the_vnc_password_is_not_visible_in_the_process_list() -> None:
    """data= would put it in ps for every user on the host."""
    script = (ISO / "test-boot.sh").read_text()

    assert "secret,id=vncsec,file=" in script


def test_the_console_binds_privately_by_default() -> None:
    """`-vnc :1` listens on every interface. On a public host that is an
    unauthenticated console on the internet, which is what it was."""
    script = (ISO / "test-boot.sh").read_text()

    assert "BIND=${BIND:-127.0.0.1}" in script


def test_the_wireshark_tools_are_installed() -> None:
    """wireshark.py refuses to start a session without all three, so an ISO
    that omits them ships a button that only ever prints an apt command.

    Checked against packages.txt rather than the script: the list moved there
    when it became something the ISO build also has to read."""
    packages = [
        line.strip()
        for line in (ROOT / "packaging" / "packages.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    for tool in ("wireshark", "xvfb", "x11vnc"):
        assert tool in packages, f"{tool} is not in the package list"


def test_dumpcap_is_allowed_to_capture_without_root() -> None:
    """The step that actually matters. wireshark-common's setuid question puts
    cap_net_admin,cap_net_raw on dumpcap and restricts it to the wireshark
    group; skip it and Wireshark starts fine and then shows no interfaces at
    all, which reads as a Labtris bug and is not one.

    Preseeded rather than asked, because a prompt in a late-command hangs an
    unattended install forever."""
    text = INSTALL_SH.read_text()

    assert "wireshark-common/install-setuid boolean true" in text
    assert "debconf-set-selections" in text


def test_the_service_account_can_use_dumpcap() -> None:
    """Capture permission is group membership, not root. The API launches
    Wireshark through `sg wireshark`, so the account it runs as has to be in
    that group or every session opens onto an empty interface list."""
    text = INSTALL_SH.read_text()

    assert 'usermod -aG wireshark "$LABTRIS_USER"' in text


PACKAGES = ROOT / "packaging" / "packages.txt"


def _packages() -> list[str]:
    return [
        line.strip()
        for line in PACKAGES.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_package_list_is_single_sourced() -> None:
    """Read twice — by the installer and by the ISO build, which downloads it
    for offline use. Two copies would drift, and the drift shows up as a
    missing binary on a machine with no network to fix it from."""
    installer = INSTALL_SH.read_text()
    build = (ISO / "build.sh").read_text()

    assert "packages.txt" in installer
    assert "packages.txt" in build


def test_the_wireshark_stack_is_in_the_package_list() -> None:
    for package in ("wireshark", "xvfb", "x11vnc"):
        assert package in _packages()


def test_the_packages_ride_on_the_disc() -> None:
    """So an install on a network with no route out still works — which is the
    normal state of a training room, not an edge case."""
    build = (ISO / "build.sh").read_text()
    commands = yaml.safe_load(AUTOINSTALL.read_text())["autoinstall"]["late-commands"]

    assert "--download-only" in build
    assert any("/cdrom/labtris-debs" in c for c in commands)
    assert any("--debs" in c for c in commands)


def test_the_installer_prefers_the_media_over_the_network() -> None:
    text = INSTALL_SH.read_text()
    local = text.index('using $(ls "$DEBS"/*.deb 2>/dev/null | wc -l) packages from the media')
    network = text.index('if [ -n "$MISSING" ]')

    assert local < network, "the network install must be the fallback, not the first choice"


def test_bundling_can_be_switched_off() -> None:
    """A macOS build host cannot produce Ubuntu packages at all, and a build
    that dies there rather than producing a network-install ISO is worse."""
    build = (ISO / "build.sh").read_text()

    assert "BUNDLE_DEBS:-1" in build
    assert "command -v debootstrap" in build


def test_the_target_needs_no_javascript_toolchain() -> None:
    """The UI is static output. Building it on the build host and shipping
    web/dist removes Node, npm and the NodeSource script from the installed
    system — three network dependencies for a few hundred kilobytes."""
    commands = yaml.safe_load(AUTOINSTALL.read_text())["autoinstall"]["late-commands"]
    joined = " ".join(commands)

    assert "nodesource" not in joined.lower()
    assert "nodejs" not in joined
    assert "web/dist" not in (ISO / "build.sh").read_text().partition("--exclude=web/dist")[1]


def test_the_python_dependencies_ride_on_the_disc() -> None:
    build = (ISO / "build.sh").read_text()
    commands = yaml.safe_load(AUTOINSTALL.read_text())["autoinstall"]["late-commands"]

    assert "pip download" in build
    assert any("/cdrom/labtris-wheels" in c for c in commands)
    assert any("--wheels" in c for c in commands)


def test_pip_is_told_never_to_reach_for_pypi() -> None:
    """--no-index is what makes it offline rather than merely preloaded; without
    it pip will happily use the network and the bundling proves nothing.
    --no-build-isolation because pip cannot fetch a build backend offline."""
    text = INSTALL_SH.read_text()

    assert "--no-index" in text
    assert "--no-build-isolation" in text


def test_apt_update_cannot_fail_the_install() -> None:
    """It fails on a machine with no route out, and with the packages on the
    media there was nothing for it to do. set -e would turn "no internet" into
    a failed install reporting a stale index."""
    text = INSTALL_SH.read_text()
    line = [ln for ln in text.splitlines() if "apt-get update" in ln][0]

    assert "||" in line, "an offline machine dies here"


def test_the_boot_test_can_cut_the_network() -> None:
    """A machine that can reach the internet will quietly use it, so an
    offline claim tested online proves nothing."""
    script = (ISO / "test-boot.sh").read_text()

    assert "NET=${NET:-user}" in script
    assert "-nic none" in script


def test_the_serial_console_can_be_reached_over_the_network() -> None:
    """VNC shows the graphical console. The serial console is the one with the
    kernel messages, and redirecting it to a file on the host makes it
    unreachable from anywhere else — which is where the person watching
    usually is."""
    script = (ISO / "test-boot.sh").read_text()

    assert "SERIAL=${SERIAL:-stdio}" in script
    assert "telnet=on" in script


def test_serial_output_is_logged_even_when_published() -> None:
    """Everything before you connect would otherwise be lost, and the
    interesting part of a boot is usually the beginning."""
    script = (ISO / "test-boot.sh").read_text()

    assert "logfile=$SERIAL_LOG" in script


def test_a_complete_media_set_means_no_network_call() -> None:
    """The bug an offline test caught and an online one never could.

    apt was called unconditionally after the media install "to confirm nothing
    is missing". With no package index it cannot resolve a name at all, so
    `apt-get install nginx` fails with "Unable to locate package" even though
    nginx is installed — and set -e took the install down with it. Only
    genuinely absent packages may be fetched."""
    text = INSTALL_SH.read_text()

    assert "MISSING" in text, "packages are not checked before fetching"
    assert 'dpkg -s "$pkg"' in text, "presence must be tested per package"
    assert 'if [ -n "$MISSING" ]' in text, "the fetch must be conditional"


def test_the_bundled_packages_are_a_repository_not_a_pile_of_files() -> None:
    """The bug that made the whole bundling useless.

    Passing loose .deb paths to apt does not work: apt resolves against its
    index, and a freshly installed target's index is whatever the ISO's base
    image shipped — months older than the build host's. It ignored the newer
    files in front of it, decided it wanted postgresql-16 16.9 rather than the
    16.15 sitting there, and went to the network. Offline that is a failure;
    online it silently works, which is why every earlier test passed.

    A Packages index makes the bundled versions candidates the solver can
    choose, and then nothing is fetched."""
    build = (ISO / "build.sh").read_text()
    installer = INSTALL_SH.read_text()

    assert "dpkg-scanpackages" in build, "no index is generated"
    assert "Packages.gz" in build
    assert "deb [trusted=yes] file:" in installer, "the media is not registered as a source"
    assert "Packages.gz" in installer, "the installer does not check for an index"


def test_a_missing_index_fails_the_build_loudly() -> None:
    """Without dpkg-scanpackages the packages ride along unusable and the
    install quietly falls back to the network — the failure this whole change
    exists to prevent, reintroduced silently."""
    build = (ISO / "build.sh").read_text()
    branch = build.partition("dpkg-scanpackages >/dev/null")[2]

    assert "die " in branch


def test_the_local_source_is_removed_after_installing() -> None:
    """The media directory is deleted when the install finishes, so an entry
    pointing at it would make every later apt-get update complain."""
    installer = INSTALL_SH.read_text()

    assert "rm -f /etc/apt/sources.list.d/labtris-local.list" in installer


def test_the_package_set_is_resolved_against_a_clean_base() -> None:
    """Not against the build host, whose installed packages change the answer.

    The first attempt walked `apt-cache depends --recurse` here and produced
    441 packages that were confidently wrong — it stopped at
    libguac-client-vnc0, a transitional name for libguac-client-vnc0t64, and
    missed that whole subtree along with the ghostscript chain. Online apt
    quietly fetched the gaps and everything looked fine. Offline it failed.

    Asking a minimal chroot of the target's own release "what would you
    install?" is the same question the target will ask, so it cannot drift."""
    build = (ISO / "build.sh").read_text()
    # Comments stripped: the explanation above names the very command it warns
    # against, and matching prose fails on the fix's own documentation. This is
    # the second test to make that mistake today.
    code = "\n".join(
        line for line in build.splitlines() if not line.lstrip().startswith("#")
    )

    assert "debootstrap" in code
    assert "apt-cache depends" not in code, "back to resolving against the build host"
    assert "chroot" in code


def test_tar_excludes_are_anchored_to_the_top_level() -> None:
    """--exclude=dist matches any component named dist, not just the one at
    the root. It silently swallowed web/dist — the prebuilt UI the whole
    no-Node-on-the-target change exists to ship — while the build cheerfully
    reported "Building the web UI" and the files sat there unshipped."""
    build = (ISO / "build.sh").read_text()
    tar = build.partition('tar -C "$ROOT"')[2].partition("-cf -")[0]

    for pattern in re.findall(r"--exclude=(\S+)", tar):
        assert pattern.startswith("./") or pattern == "__pycache__", (
            f"--exclude={pattern} is unanchored and will match nested paths too"
        )


def test_the_build_checks_the_ui_actually_reached_the_staging_tree() -> None:
    """Building it and shipping it are different things, and the gap between
    them was invisible for a whole build cycle."""
    build = (ISO / "build.sh").read_text()

    assert 'web/dist/index.html' in build
    assert "did not reach the staging tree" in build


def test_the_installer_keeps_the_prebuilt_ui() -> None:
    """Two tars copy this tree — the ISO build's and the installer's — and both
    excluded web/dist. Fixing one left the other, so the ISO carried the UI
    correctly and the installer discarded it on the way to /opt/labtris. The
    API then answered JSON and a 404 where the interface should have been."""
    text = INSTALL_SH.read_text()
    tar = text.partition('tar -C "$SOURCE"')[2].partition("-cf -")[0]

    assert "web/dist" not in tar, "the installer throws away the prebuilt UI"


def test_the_service_account_can_open_dev_kvm() -> None:
    """/dev/kvm is root:kvm 0660, so acceleration is a group membership like
    docker and wireshark. Without it QEMU dies at startup with "failed to
    initialize kvm: Permission denied" — only on hosts that actually have KVM,
    which is why a development box with none never revealed it."""
    text = INSTALL_SH.read_text()

    assert 'usermod -aG kvm "$LABTRIS_USER"' in text
