defmodule Shellshare.RoomTest do
  use ExUnit.Case, async: true

  alias Shellshare.Room

  setup do
    # Start required services
    start_supervised!({Registry, keys: :unique, name: Shellshare.RoomRegistry})
    start_supervised!({DynamicSupervisor, strategy: :one_for_one, name: Shellshare.RoomSupervisor})
    start_supervised!({Phoenix.PubSub, name: Shellshare.PubSub})
    :ok
  end

  describe "start/2" do
    test "creates a new room with given name and secret" do
      assert {:ok, pid} = Room.start("test-room", "secret123")
      assert is_pid(pid)
      assert Process.alive?(pid)
    end

    test "returns error if room with same name already exists" do
      {:ok, pid1} = Room.start("duplicate-room", "secret1")
      assert {:error, {:already_started, ^pid1}} = Room.start("duplicate-room", "secret2")
    end
  end

  describe "lookup/1" do
    test "returns {:ok, pid} for existing room" do
      {:ok, pid} = Room.start("lookup-test", "secret")
      assert {:ok, ^pid} = Room.lookup("lookup-test")
    end

    test "returns {:error, :not_found} for non-existent room" do
      assert {:error, :not_found} = Room.lookup("does-not-exist")
    end
  end

  describe "get_or_create/2" do
    test "creates new room if it doesn't exist" do
      assert {:ok, pid} = Room.get_or_create("new-room", "secret")
      assert Process.alive?(pid)
    end

    test "returns existing room if secret matches" do
      {:ok, pid1} = Room.start("existing-room", "correct-secret")
      assert {:ok, ^pid1} = Room.get_or_create("existing-room", "correct-secret")
    end

    test "returns error if secret doesn't match" do
      {:ok, _pid} = Room.start("auth-room", "correct-secret")
      assert {:error, :unauthorized} = Room.get_or_create("auth-room", "wrong-secret")
    end
  end

  describe "authorized?/2" do
    test "returns true for correct secret" do
      {:ok, pid} = Room.start("auth-test", "my-secret")
      assert Room.authorized?(pid, "my-secret") == true
    end

    test "returns false for incorrect secret" do
      {:ok, pid} = Room.start("auth-test-2", "my-secret")
      assert Room.authorized?(pid, "wrong-secret") == false
    end
  end

  describe "push/2" do
    test "updates room buffer with message" do
      {:ok, pid} = Room.start("push-test", "secret")
      
      message = Base.encode64(URI.encode("Hello, World!"))
      Room.push(pid, %{"message" => message, "size" => %{"cols" => 80, "rows" => 24}})
      
      state = Room.get_state(pid)
      decoded = state.buffer |> Base.decode64!() |> URI.decode()
      assert decoded == "Hello, World!"
    end

    test "concatenates multiple messages" do
      {:ok, pid} = Room.start("concat-test", "secret")
      
      msg1 = Base.encode64(URI.encode("Hello, "))
      msg2 = Base.encode64(URI.encode("World!"))
      
      Room.push(pid, %{"message" => msg1, "size" => %{"cols" => 80, "rows" => 24}})
      Room.push(pid, %{"message" => msg2, "size" => %{"cols" => 80, "rows" => 24}})
      
      state = Room.get_state(pid)
      decoded = state.buffer |> Base.decode64!() |> URI.decode()
      assert decoded == "Hello, World!"
    end

    test "updates terminal size" do
      {:ok, pid} = Room.start("size-test", "secret")
      
      message = Base.encode64(URI.encode("test"))
      Room.push(pid, %{"message" => message, "size" => %{"cols" => 120, "rows" => 40}})
      
      state = Room.get_state(pid)
      assert state.size == %{cols: 120, rows: 40}
    end

    test "broadcasts update to subscribers" do
      {:ok, pid} = Room.start("broadcast-test", "secret")
      Room.subscribe("broadcast-test")
      
      message = Base.encode64(URI.encode("test message"))
      Room.push(pid, %{"message" => message, "size" => %{"cols" => 80, "rows" => 24}})
      
      assert_receive {:room_update, %{message: ^message, size: %{cols: 80, rows: 24}}}
    end
  end

  describe "get_state/1" do
    test "returns room state" do
      {:ok, pid} = Room.start("state-test", "secret123")
      
      state = Room.get_state(pid)
      assert state.name == "state-test"
      assert state.secret == "secret123"
      assert state.buffer == ""
      assert state.size == %{cols: 80, rows: 24}
    end
  end

  describe "stop/1" do
    test "stops the room process" do
      {:ok, pid} = Room.start("stop-test", "secret")
      assert Process.alive?(pid)
      
      Room.stop(pid)
      
      # Give it a moment to stop
      Process.sleep(10)
      refute Process.alive?(pid)
    end

    test "broadcasts room_closed to subscribers" do
      {:ok, pid} = Room.start("close-broadcast-test", "secret")
      Room.subscribe("close-broadcast-test")
      
      Room.stop(pid)
      
      assert_receive :room_closed
    end
  end

  describe "list_rooms/0" do
    test "returns list of all active room names" do
      {:ok, _} = Room.start("room-a", "secret")
      {:ok, _} = Room.start("room-b", "secret")
      
      rooms = Room.list_rooms()
      assert "room-a" in rooms
      assert "room-b" in rooms
    end
  end
end
