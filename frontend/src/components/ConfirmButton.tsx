import { AlertDialog, Button, Flex } from "@radix-ui/themes";
import type { ReactNode } from "react";

export function ConfirmButton({
  label,
  title,
  description,
  confirmLabel = "确认",
  color = "red",
  disabled,
  onConfirm,
  children,
}: {
  label?: string;
  title: string;
  description: string;
  confirmLabel?: string;
  color?: "red" | "amber" | "blue";
  disabled?: boolean;
  onConfirm: () => void;
  children?: ReactNode;
}) {
  return (
    <AlertDialog.Root>
      <AlertDialog.Trigger>
        <Button size="1" variant="soft" color={color} disabled={disabled}>
          {children ?? label}
        </Button>
      </AlertDialog.Trigger>
      <AlertDialog.Content maxWidth="440px">
        <AlertDialog.Title>{title}</AlertDialog.Title>
        <AlertDialog.Description size="2">{description}</AlertDialog.Description>
        <Flex gap="3" mt="4" justify="end">
          <AlertDialog.Cancel>
            <Button variant="soft" color="gray">
              取消
            </Button>
          </AlertDialog.Cancel>
          <AlertDialog.Action>
            <Button color={color} onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </AlertDialog.Action>
        </Flex>
      </AlertDialog.Content>
    </AlertDialog.Root>
  );
}
