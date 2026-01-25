defmodule ShellshareWeb.RoomLive do
  @moduledoc """
  LiveView for viewing a shared terminal session.

  Subscribes to room updates via PubSub and renders
  a terminal emulator using xterm.js.
  """

  use ShellshareWeb, :live_view

  alias Shellshare.Room

  @impl true
  def mount(%{"room" => room_name}, _session, socket) do
    if connected?(socket) do
      # Subscribe to room updates
      Room.subscribe(room_name)

      # Track presence for user count
      track_presence(socket, room_name)
    end

    # Try to get existing room state
    {buffer, size} = get_room_state(room_name)

    socket =
      socket
      |> assign(:room_name, room_name)
      |> assign(:buffer, buffer)
      |> assign(:size, size)
      |> assign(:user_count, get_user_count(room_name))
      |> assign(:room_active, buffer != "")
      |> assign(:page_title, "#{room_name} - shellshare")

    {:ok, socket}
  end

  @impl true
  def handle_info({:room_update, %{message: message, size: size}}, socket) do
    socket =
      socket
      |> assign(:room_active, true)
      |> assign(:size, size)
      |> push_event("terminal:data", %{message: message, size: size})

    {:noreply, socket}
  end

  @impl true
  def handle_info(:room_closed, socket) do
    socket =
      socket
      |> assign(:room_active, false)
      |> push_event("terminal:closed", %{})

    {:noreply, socket}
  end

  @impl true
  def handle_info(%{event: "presence_diff"}, socket) do
    user_count = get_user_count(socket.assigns.room_name)
    {:noreply, assign(socket, :user_count, user_count)}
  end

  @impl true
  def handle_info(_msg, socket) do
    {:noreply, socket}
  end

  defp get_room_state(room_name) do
    case Room.lookup(room_name) do
      {:ok, pid} ->
        state = Room.get_state(pid)
        {state.buffer, state.size}

      {:error, :not_found} ->
        {"", %{cols: 80, rows: 24}}
    end
  end

  defp track_presence(socket, room_name) do
    topic = "room_presence:#{room_name}"

    # Use simple tracking with PubSub
    Phoenix.PubSub.subscribe(Shellshare.PubSub, topic)

    # Track this viewer
    :pg.join(:shellshare_viewers, room_name, self())
  end

  defp get_user_count(room_name) do
    try do
      :pg.get_members(:shellshare_viewers, room_name)
      |> length()
    rescue
      _ -> 1
    end
  end

  @impl true
  def render(assigns) do
    ~H"""
    <div class="min-h-screen bg-gray-900 text-white">
      <header class="bg-gray-800 border-b border-gray-700 px-4 py-3">
        <div class="flex items-center justify-between max-w-7xl mx-auto">
          <div class="flex items-center space-x-4">
            <a href="/" class="text-xl font-bold hover:text-gray-300">shellshare</a>
            <span class="text-gray-500">/</span>
            <span class="text-gray-300"><%= @room_name %></span>
          </div>
          <div class="flex items-center space-x-4">
            <div class="flex items-center space-x-2 text-sm text-gray-400">
              <span class="inline-block w-2 h-2 rounded-full bg-green-500 animate-pulse" :if={@room_active}></span>
              <span class="inline-block w-2 h-2 rounded-full bg-gray-500" :if={!@room_active}></span>
              <span><%= @user_count %> viewer<%= if @user_count != 1, do: "s" %></span>
            </div>
          </div>
        </div>
      </header>

      <main class="p-4">
        <div class="max-w-7xl mx-auto">
          <div
            id="terminal-container"
            class="bg-black rounded-lg overflow-hidden"
            phx-hook="Terminal"
            data-room={@room_name}
            data-buffer={@buffer}
            data-cols={@size.cols}
            data-rows={@size.rows}
          >
            <div :if={!@room_active} class="flex items-center justify-center h-96 text-gray-500">
              <div class="text-center">
                <p class="text-lg">Waiting for broadcaster...</p>
                <p class="text-sm mt-2">The terminal will appear when someone starts sharing.</p>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
    """
  end
end
