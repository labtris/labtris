from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LABTRIS_", extra="ignore")

    database_url: str = "postgresql+asyncpg://pnl:pnl@localhost/pnl"
    netd_socket: str = "/run/labtris/netd.sock"
    docker_host: str = "unix:///var/run/docker.sock"
    log_level: str = "info"

    # QEMU backend. "tcg" (software emulation) is the safe default — nested KVM
    # is unreliable on generic cloud VMs (some silently hang on first vcpu run
    # despite /dev/kvm + kvm-ok reporting success). Set LABTRIS_QEMU_ACCEL=kvm on a
    # host where nested virtualization is verified to actually execute.
    qemu_accel: str = "tcg"
    #: Where the session signing key lives when one is not configured. Kept
    #: outside the repo so it is not committed by accident.
    session_secret: str = ""
    session_secret_path: str = "~/.config/labtris/session-secret"
    #: Whether a node's extra_args are passed to QEMU. Off by default: they are
    #: unrestricted arguments to a process on this host, which is a very
    #: different trust level from the rest of the node options.
    qemu_allow_extra_args: bool = False
    qemu_vm_dir: str = "~/.local/share/labtris/qemu-vms"
    qemu_image_cache_dir: str = "~/.cache/labtris/qemu-images"
    #: Where lab snapshots (`labtris-pod-v1.tar.gz`) are written on save
    #: and read from on load. Kept under $HOME/.local/share by default so
    #: it inherits the same "user data" retention as the QEMU VMs — an
    #: apt upgrade or a systemd restart never trims it, and a full-disk
    #: cleanup is one directory to eyeball.
    pod_dir: str = "~/.local/share/labtris/pods"

    # The assistant talks to an OpenAI-compatible endpoint — LiteLLM by
    # default, so whichever provider you route there is your business and no
    # key ever leaves your network. Empty api key is allowed: a local LiteLLM
    # or Ollama usually needs none.
    llm_base_url: str = "http://127.0.0.1:4000"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    # Runaway backstop, not a normal limit. The loop terminates naturally
    # when the model returns without tool calls — same as Claude Code.
    # This ceiling only kicks in if the model gets stuck calling tools
    # forever (a real thing that has happened in production LLMs). Set it
    # high enough that no real debugging session ever hits it (Claude
    # Code sessions of 200+ tool calls are unremarkable). If a user
    # wants a tighter safety net for a specific instance, lower via
    # LABTRIS_LLM_MAX_STEPS. The Stop button in the UI is the primary
    # user-controlled interruption.
    llm_max_steps: int = 500

    # The assistant drives this instance through its own public API, reusing
    # the MCP tool definitions rather than keeping a second implementation of
    # "create a node" that drifts from the real one.
    self_url: str = "http://127.0.0.1:8080"


settings = Settings()
