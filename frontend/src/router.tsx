import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";
import { AppShell } from "./components/AppShell";
import { DownloadsPage } from "./pages/DownloadsPage";
import { DownloadJobPage } from "./pages/DownloadJobPage";
import { LineagePage } from "./pages/LineagePage";
import { LineageRunPage } from "./pages/LineageRunPage";
import { AccountsPage } from "./pages/AccountsPage";
import { SettingsPage } from "./pages/SettingsPage";

const rootRoute = createRootRoute({ component: AppShell });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/downloads" });
  },
});

const downloadsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/downloads",
  component: DownloadsPage,
});

const downloadJobRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/downloads/$jobId",
  component: DownloadJobPage,
});

const lineageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/lineage",
  component: LineagePage,
});

const lineageRunRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/lineage/$runId",
  component: LineageRunPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const accountsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/accounts",
  component: AccountsPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  downloadsRoute,
  downloadJobRoute,
  lineageRoute,
  lineageRunRoute,
  settingsRoute,
  accountsRoute,
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
