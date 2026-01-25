defmodule Shellshare.Application do
  @moduledoc false

  use Application

  @impl true
  def start(_type, _args) do
    # Start :pg for viewer presence tracking
    :pg.start_link(:shellshare_viewers)

    children = [
      ShellshareWeb.Telemetry,
      {Phoenix.PubSub, name: Shellshare.PubSub},
      # Room supervisor and registry
      {Registry, keys: :unique, name: Shellshare.RoomRegistry},
      {DynamicSupervisor, strategy: :one_for_one, name: Shellshare.RoomSupervisor},
      # Periodic room cleanup
      Shellshare.RoomCleaner,
      # Start the endpoint (http/https)
      ShellshareWeb.Endpoint
    ]

    opts = [strategy: :one_for_one, name: Shellshare.Supervisor]
    Supervisor.start_link(children, opts)
  end

  @impl true
  def config_change(changed, _new, removed) do
    ShellshareWeb.Endpoint.config_change(changed, removed)
    :ok
  end
end
