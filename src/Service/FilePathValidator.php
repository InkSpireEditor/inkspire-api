<?php

namespace App\Service;

class FilePathValidator
{
    public function __construct(
        private readonly string $projectRoot,
        private readonly string $basePathRelative,
    ) {}

    public function getStorageRoot(): string
    {
        return $this->projectRoot . '/' . $this->basePathRelative;
    }

    /**
     * Throws if $path resolves to a location outside the configured storage root.
     * Must be called before any filesystem I/O on a path read from the database.
     */
    public function assertWithinStorageRoot(string $path): void
    {
        $storageRoot = realpath($this->projectRoot . '/' . $this->basePathRelative);
        if ($storageRoot === false) {
            throw new \RuntimeException('Storage root directory does not exist');
        }

        $resolvedDir = realpath(dirname($path));
        if ($resolvedDir === false) {
            throw new \RuntimeException(sprintf('Path escapes storage root (directory not found): %s', $path));
        }

        if (!str_starts_with($resolvedDir . DIRECTORY_SEPARATOR, $storageRoot . DIRECTORY_SEPARATOR)) {
            throw new \RuntimeException(sprintf('Path escapes storage root: %s', $path));
        }
    }
}
