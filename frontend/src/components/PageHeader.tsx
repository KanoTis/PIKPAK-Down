import type { ReactNode } from "react";
import { Flex, Heading, Text } from "@radix-ui/themes";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <Heading size="6" as="h1">
          {title}
        </Heading>
        {description ? (
          <Text as="p" size="2" color="gray" mt="2">
            {description}
          </Text>
        ) : null}
      </div>
      {actions ? <Flex gap="2" wrap="wrap">{actions}</Flex> : null}
    </div>
  );
}
