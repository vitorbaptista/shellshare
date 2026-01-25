defmodule Shellshare.RoomCleaner do
  @moduledoc """
  Periodically cleans up inactive rooms based on TTL.
  """

  use GenServer
  require Logger

  @check_interval :timer.minutes(1)

  def start_link(_opts) do
    GenServer.start_link(__MODULE__, [], name: __MODULE__)
  end

  @impl true
  def init(_) do
    schedule_check()
    {:ok, %{}}
  end

  @impl true
  def handle_info(:check_rooms, state) do
    cleanup_inactive_rooms()
    schedule_check()
    {:noreply, state}
  end

  defp schedule_check do
    Process.send_after(self(), :check_rooms, @check_interval)
  end

  defp cleanup_inactive_rooms do
    ttl_ms = Application.get_env(:shellshare, :room)[:ttl_ms] || 5 * 60 * 1000
    ttl_seconds = div(ttl_ms, 1000)
    now = DateTime.utc_now()

    Shellshare.Room.list_rooms()
    |> Enum.each(fn room_name ->
      case Shellshare.Room.lookup(room_name) do
        {:ok, pid} ->
          state = Shellshare.Room.get_state(pid)
          age = DateTime.diff(now, state.updated_at, :second)

          if age > ttl_seconds do
            Logger.info("Cleaning up inactive room: #{room_name} (inactive for #{age}s)")
            Shellshare.Room.stop(pid)
          end

        {:error, :not_found} ->
          :ok
      end
    end)
  end
end
