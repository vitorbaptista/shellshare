defmodule ShellshareWeb.PageHTML do
  @moduledoc """
  This module contains pages rendered by PageController.
  """
  use ShellshareWeb, :html

  embed_templates "page_html/*"
end
