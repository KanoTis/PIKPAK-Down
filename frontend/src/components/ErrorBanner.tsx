import { Callout } from "@radix-ui/themes";
import { AlertTriangle } from "lucide-react";

export function ErrorBanner({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return (
    <div role="alert" aria-live="polite" style={{ marginBottom: 16 }}>
      <Callout.Root color="red" size="1">
        <Callout.Icon>
          <AlertTriangle size={16} aria-hidden />
        </Callout.Icon>
        <Callout.Text>{message}</Callout.Text>
      </Callout.Root>
    </div>
  );
}

export function InfoBanner({ message }: { message: string }) {
  return (
    <div className="warning-strip" role="status" aria-live="polite">
      <AlertTriangle size={15} aria-hidden />
      <span>{message}</span>
    </div>
  );
}
