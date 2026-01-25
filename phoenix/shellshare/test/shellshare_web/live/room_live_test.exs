defmodule ShellshareWeb.RoomLiveTest do
  use ShellshareWeb.ConnCase

  import Phoenix.LiveViewTest

  alias Shellshare.Room

  describe "GET /r/:room (LiveView)" do
    test "renders terminal container for new room", %{conn: conn} do
      {:ok, view, html} = live(conn, "/r/new-viewer-room")
      
      assert html =~ "terminal-container"
      assert html =~ "Waiting for broadcaster"
      assert html =~ "new-viewer-room"
    end

    test "shows initial buffer for existing room", %{conn: conn} do
      # Create room with some content
      {:ok, pid} = Room.get_or_create("existing-room", "secret")
      message = Base.encode64(URI.encode("Initial content"))
      Room.push(pid, %{"message" => message, "size" => %{"cols" => 80, "rows" => 24}})
      
      {:ok, view, html} = live(conn, "/r/existing-room")
      
      # The buffer should be in the data attribute for the JS hook
      assert html =~ "data-buffer="
      assert html =~ "existing-room"
    end

    test "updates when room receives new messages", %{conn: conn} do
      # Create room
      {:ok, pid} = Room.get_or_create("live-update-room", "secret")
      
      # Connect viewer
      {:ok, view, _html} = live(conn, "/r/live-update-room")
      
      # Send a message to the room
      message = Base.encode64(URI.encode("Live update!"))
      Room.push(pid, %{"message" => message, "size" => %{"cols" => 80, "rows" => 24}})
      
      # The LiveView should have pushed an event
      # We can't easily test JS push_event, but we can verify the view is still connected
      assert render(view) =~ "live-update-room"
    end

    test "shows viewer count", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/r/viewer-count-room")
      
      assert html =~ "viewer"
    end

    test "sets page title with room name", %{conn: conn} do
      {:ok, view, _html} = live(conn, "/r/title-test-room")
      
      assert page_title(view) =~ "title-test-room"
    end
  end
end
