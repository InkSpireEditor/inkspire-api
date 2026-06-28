<?php

namespace App\Service;

class FileStorageService implements FileStorageServiceInterface
{
    public function __construct(private readonly FilePathValidator $pathValidator)
    {
    }

    public function create(string $filename): void
    {
        $path = $this->resolvePath($filename);
        $this->ensureDirectoryExists($path);
        file_put_contents($path, '');
    }

    public function read(string $filename): string
    {
        $path = $this->resolvePath($filename);
        $content = file_get_contents($path);
        if ($content === false) {
            throw new \RuntimeException(sprintf('Failed to read file: %s', $path));
        }
        return $content;
    }

    public function write(string $filename, string $content): void
    {
        $path = $this->resolvePath($filename);
        $this->ensureDirectoryExists($path);
        if (file_put_contents($path, $content) === false) {
            throw new \RuntimeException(sprintf('Failed to write file: %s', $path));
        }
    }

    public function rename(string $oldFilename, string $newFilename): void
    {
        $oldPath = $this->resolvePath($oldFilename);
        $newPath = $this->resolvePath($newFilename);
        if (file_exists($oldPath) && !rename($oldPath, $newPath)) {
            throw new \RuntimeException(sprintf('Failed to rename %s to %s', $oldPath, $newPath));
        }
    }

    public function delete(string $filename): void
    {
        $path = $this->resolvePath($filename);
        if (file_exists($path) && !unlink($path)) {
            throw new \RuntimeException(sprintf('Failed to delete file: %s', $path));
        }
    }

    public function exists(string $filename): bool
    {
        $path = $this->resolvePath($filename);
        return file_exists($path);
    }

    /**
     * Resolves a relative filename to its absolute storage path and validates it
     * is within the configured storage root. Absolute paths are rejected — the entity
     * must store only the filename, never a full path.
     */
    private function resolvePath(string $filename): string
    {
        if (str_starts_with($filename, '/')) {
            throw new \RuntimeException(sprintf('Expected a relative filename, got an absolute path: %s', $filename));
        }
        $path = $this->pathValidator->getStorageRoot() . '/' . $filename;
        $this->pathValidator->assertWithinStorageRoot($path);
        return $path;
    }

    private function ensureDirectoryExists(string $absolutePath): void
    {
        $dir = dirname($absolutePath);
        if (!is_dir($dir)) {
            mkdir($dir, 0755, true);
        }
    }
}
