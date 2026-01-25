defmodule ShellshareWeb.RoomControllerTest do
  use ShellshareWeb.ConnCase

  alias Shellshare.Room

  describe "POST /r/:room" do
    test "creates room and accepts message with valid auth", %{conn: conn} do
      message = Base.encode64(URI.encode("Hello!"))
      
      conn =
        conn
        |> put_req_header("authorization", "my-secret")
        |> put_req_header("content-type", "application/json")
        |> post("/r/test-room", %{
          "message" => message,
          "size" => %{"cols" => 80, "rows" => 24}
        })
      
      assert response(conn, 200) == "OK"
      
      # Verify room was created and message stored
      {:ok, pid} = Room.lookup("test-room")
      state = Room.get_state(pid)
      assert state.buffer |> Base.decode64!() |> URI.decode() == "Hello!"
    end

    test "accepts subsequent messages with matching auth", %{conn: conn} do
      # First request creates the room
      msg1 = Base.encode64(URI.encode("Part 1 "))
      conn
      |> put_req_header("authorization", "secret123")
      |> put_req_header("content-type", "application/json")
      |> post("/r/multi-msg-room", %{
        "message" => msg1,
        "size" => %{"cols" => 80, "rows" => 24}
      })
      
      # Second request appends to buffer
      msg2 = Base.encode64(URI.encode("Part 2"))
      conn =
        conn
        |> put_req_header("authorization", "secret123")
        |> put_req_header("content-type", "application/json")
        |> post("/r/multi-msg-room", %{
          "message" => msg2,
          "size" => %{"cols" => 80, "rows" => 24}
        })
      
      assert response(conn, 200) == "OK"
      
      {:ok, pid} = Room.lookup("multi-msg-room")
      state = Room.get_state(pid)
      assert state.buffer |> Base.decode64!() |> URI.decode() == "Part 1 Part 2"
    end

    test "rejects request with wrong secret for existing room", %{conn: conn} do
      # Create room with one secret
      {:ok, _pid} = Room.get_or_create("protected-room", "correct-secret")
      
      # Try to post with wrong secret
      conn =
        conn
        |> put_req_header("authorization", "wrong-secret")
        |> put_req_header("content-type", "application/json")
        |> post("/r/protected-room", %{
          "message" => Base.encode64("data"),
          "size" => %{"cols" => 80, "rows" => 24}
        })
      
      assert response(conn, 401) == "Unauthorized"
    end

    test "rejects request without authorization header", %{conn: conn} do
      conn =
        conn
        |> put_req_header("content-type", "application/json")
        |> post("/r/no-auth-room", %{
          "message" => Base.encode64("data"),
          "size" => %{"cols" => 80, "rows" => 24}
        })
      
      assert response(conn, 401) == "Unauthorized"
    end

    test "rejects request with empty authorization header", %{conn: conn} do
      conn =
        conn
        |> put_req_header("authorization", "")
        |> put_req_header("content-type", "application/json")
        |> post("/r/empty-auth-room", %{
          "message" => Base.encode64("data"),
          "size" => %{"cols" => 80, "rows" => 24}
        })
      
      assert response(conn, 401) == "Unauthorized"
    end
  end

  describe "DELETE /r/:room" do
    test "deletes room with valid auth", %{conn: conn} do
      # Create room first
      {:ok, pid} = Room.get_or_create("delete-me", "my-secret")
      assert Process.alive?(pid)
      
      conn =
        conn
        |> put_req_header("authorization", "my-secret")
        |> delete("/r/delete-me")
      
      assert response(conn, 202) == "Accepted"
      
      # Room should be gone
      Process.sleep(10)
      assert {:error, :not_found} = Room.lookup("delete-me")
    end

    test "rejects delete with wrong secret", %{conn: conn} do
      {:ok, pid} = Room.get_or_create("dont-delete", "correct-secret")
      
      conn =
        conn
        |> put_req_header("authorization", "wrong-secret")
        |> delete("/r/dont-delete")
      
      assert response(conn, 401) == "Unauthorized"
      
      # Room should still exist
      assert Process.alive?(pid)
    end

    test "returns 202 for non-existent room", %{conn: conn} do
      conn =
        conn
        |> put_req_header("authorization", "any-secret")
        |> delete("/r/nonexistent")
      
      assert response(conn, 202) == "Accepted"
    end
  end
end
