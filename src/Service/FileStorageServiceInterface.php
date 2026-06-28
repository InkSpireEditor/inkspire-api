<?php

namespace App\Service;

interface FileStorageServiceInterface
{
    public function create(string $path): void;

    public function read(string $path): string;

    public function write(string $path, string $content): void;

    public function rename(string $oldPath, string $newPath): void;

    public function delete(string $path): void;

    public function exists(string $path): bool;
}
