defmodule Shellshare.Room do
  @moduledoc """
  GenServer that manages the state of a single room.

  Each room stores:
  - Terminal buffer (base64 encoded messages concatenated)
  - Terminal size (cols x rows)
  - Secret for authorization
  - Last activity timestamp for TTL
  """

  use GenServer
  require Logger

  @registry Shellshare.RoomRegistry
  @supervisor Shellshare.RoomSupervisor
  @pubsub Shellshare.PubSub

  defstruct [:name, :secret, :buffer, :size, :created_at, :updated_at]

  @type t :: %__MODULE__{
          name: String.t(),
          secret: String.t(),
          buffer: binary(),
          size: %{cols: non_neg_integer(), rows: non_neg_integer()},
          created_at: DateTime.t(),
          updated_at: DateTime.t()
        }

  # Client API

  @doc """
  Starts a new room with the given name and secret.
  Returns {:ok, pid} or {:error, {:already_started, pid}}.
  """
  @spec start(String.t(), String.t()) :: {:ok, pid()} | {:error, term()}
  def start(name, secret) do
    DynamicSupervisor.start_child(
      @supervisor,
      {__MODULE__, {name, secret}}
    )
  end

  @doc """
  Gets or creates a room. If the room exists and the secret matches,
  returns {:ok, pid}. If the secret doesn't match, returns {:error, :unauthorized}.
  """
  @spec get_or_create(String.t(), String.t()) :: {:ok, pid()} | {:error, :unauthorized}
  def get_or_create(name, secret) do
    case lookup(name) do
      {:ok, pid} ->
        # Room exists, verify secret
        if authorized?(pid, secret) do
          {:ok, pid}
        else
          {:error, :unauthorized}
        end

      {:error, :not_found} ->
        # Create new room
        case start(name, secret) do
          {:ok, pid} -> {:ok, pid}
          {:error, {:already_started, pid}} ->
            # Race condition - check auth again
            if authorized?(pid, secret) do
              {:ok, pid}
            else
              {:error, :unauthorized}
            end
        end
    end
  end

  @doc """
  Looks up a room by name.
  """
  @spec lookup(String.t()) :: {:ok, pid()} | {:error, :not_found}
  def lookup(name) do
    case Registry.lookup(@registry, name) do
      [{pid, _}] -> {:ok, pid}
      [] -> {:error, :not_found}
    end
  end

  @doc """
  Checks if the given secret is authorized for this room.
  """
  @spec authorized?(pid(), String.t()) :: boolean()
  def authorized?(pid, secret) do
    GenServer.call(pid, {:authorized?, secret})
  end

  @doc """
  Pushes a new message to the room's buffer.
  Broadcasts the update to all subscribers.
  """
  @spec push(pid(), map()) :: :ok
  def push(pid, %{"message" => message, "size" => size}) do
    GenServer.call(pid, {:push, message, size})
  end

  @doc """
  Gets the current state of the room.
  """
  @spec get_state(pid()) :: t()
  def get_state(pid) do
    GenServer.call(pid, :get_state)
  end

  @doc """
  Stops the room (called on DELETE).
  """
  @spec stop(pid()) :: :ok
  def stop(pid) do
    GenServer.stop(pid, :normal)
  end

  @doc """
  Subscribes to room updates via PubSub.
  """
  @spec subscribe(String.t()) :: :ok | {:error, term()}
  def subscribe(room_name) do
    Phoenix.PubSub.subscribe(@pubsub, topic(room_name))
  end

  @doc """
  Returns the list of all active room names.
  """
  @spec list_rooms() :: [String.t()]
  def list_rooms do
    Registry.select(@registry, [{{:"$1", :_, :_}, [], [:"$1"]}])
  end

  # Server Callbacks

  def start_link({name, secret}) do
    GenServer.start_link(__MODULE__, {name, secret}, name: via(name))
  end

  def child_spec({name, secret}) do
    %{
      id: {__MODULE__, name},
      start: {__MODULE__, :start_link, [{name, secret}]},
      restart: :temporary
    }
  end

  @impl true
  def init({name, secret}) do
    now = DateTime.utc_now()

    state = %__MODULE__{
      name: name,
      secret: secret,
      buffer: "",
      size: %{cols: 80, rows: 24},
      created_at: now,
      updated_at: now
    }

    Logger.info("Room started: #{name}")
    {:ok, state}
  end

  @impl true
  def handle_call({:authorized?, secret}, _from, state) do
    {:reply, state.secret == secret, state}
  end

  @impl true
  def handle_call({:push, message, size}, _from, state) do
    # Decode and re-encode to concatenate properly
    # The Node.js version stores each message separately and joins them later
    # We'll do the same by accumulating the decoded content

    decoded_existing = decode_buffer(state.buffer)
    decoded_new = decode_message(message)
    combined = decoded_existing <> decoded_new

    # Apply max buffer size limit
    max_size = Application.get_env(:shellshare, :room)[:max_buffer_size] || 1024 * 1024
    trimmed = if byte_size(combined) > max_size do
      # Keep the last max_size bytes
      binary_part(combined, byte_size(combined) - max_size, max_size)
    else
      combined
    end

    # Re-encode
    new_buffer = encode_buffer(trimmed)

    new_size = normalize_size(size)
    now = DateTime.utc_now()

    new_state = %{state | buffer: new_buffer, size: new_size, updated_at: now}

    # Broadcast update to all LiveView subscribers
    broadcast(state.name, {:room_update, %{message: message, size: new_size}})

    {:reply, :ok, new_state}
  end

  @impl true
  def handle_call(:get_state, _from, state) do
    {:reply, state, state}
  end

  @impl true
  def terminate(reason, state) do
    Logger.info("Room stopped: #{state.name}, reason: #{inspect(reason)}")
    # Notify subscribers that room is closed
    broadcast(state.name, :room_closed)
    :ok
  end

  # Private Helpers

  defp via(name) do
    {:via, Registry, {@registry, name}}
  end

  defp topic(name) do
    "room:#{name}"
  end

  defp broadcast(room_name, message) do
    Phoenix.PubSub.broadcast(@pubsub, topic(room_name), message)
  end

  # Decodes base64 -> URL-decoded content
  defp decode_message(message) when is_binary(message) do
    case Base.decode64(message) do
      {:ok, url_encoded} -> URI.decode(url_encoded)
      :error -> ""
    end
  end
  defp decode_message(_), do: ""

  defp decode_buffer(""), do: ""
  defp decode_buffer(buffer) do
    case Base.decode64(buffer) do
      {:ok, decoded} -> URI.decode(decoded)
      :error -> ""
    end
  end

  defp encode_buffer(content) do
    content
    |> URI.encode()
    |> Base.encode64()
  end

  defp normalize_size(%{"cols" => cols, "rows" => rows}) when is_integer(cols) and is_integer(rows) do
    %{cols: cols, rows: rows}
  end
  defp normalize_size(%{cols: cols, rows: rows}) when is_integer(cols) and is_integer(rows) do
    %{cols: cols, rows: rows}
  end
  defp normalize_size(_), do: %{cols: 80, rows: 24}
end
