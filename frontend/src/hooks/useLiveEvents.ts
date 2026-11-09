import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

const EVENT_TYPES = ["download_job", "download_task", "account", "lineage_run", "cleanup"];

export function useLiveEvents() {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (typeof window === "undefined" || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events");
    const handler = (event: MessageEvent) => {
      let payload: { entity_type?: string; entity_id?: string } = {};
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (payload.entity_type) {
        case "download_job":
          void queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
          void queryClient.invalidateQueries({ queryKey: ["download-job"] });
          break;
        case "download_task":
          void queryClient.invalidateQueries({ queryKey: ["download-job"] });
          void queryClient.invalidateQueries({ queryKey: ["download-task"] });
          void queryClient.invalidateQueries({ queryKey: ["job-files"] });
          break;
        case "account":
          void queryClient.invalidateQueries({ queryKey: ["accounts"] });
          void queryClient.invalidateQueries({ queryKey: ["cleanups"] });
          break;
        case "lineage_run":
          void queryClient.invalidateQueries({ queryKey: ["lineage-runs"] });
          void queryClient.invalidateQueries({ queryKey: ["lineage-run"] });
          break;
        case "cleanup":
          void queryClient.invalidateQueries({ queryKey: ["cleanups"] });
          break;
        default:
          break;
      }
    };
    for (const type of EVENT_TYPES) source.addEventListener(type, handler as EventListener);
    return () => source.close();
  }, [queryClient]);
}
