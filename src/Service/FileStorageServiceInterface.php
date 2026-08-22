<?php

namespace App\Service;

/**
 * Reads and writes `.ink` files under the configured storage root.
 *
 * Every method takes a bare filename as stored in File::$path, never a full
 * path. Implementations resolve it against the storage root and reject anything
 * that would escape it, so callers must not pass an absolute path or build one
 * themselves.
 */
interface FileStorageServiceInterface
{
    /** Creates an empty file, replacing any existing one at the same name. */
    public function create(string $filename): void;

    /** @throws \RuntimeException if the file cannot be read. */
    public function read(string $filename): string;

    /** @throws \RuntimeException if the file cannot be written. */
    public function write(string $filename, string $content): void;

    /** Renames a file. Does nothing if the source does not exist. */
    public function rename(string $oldFilename, string $newFilename): void;

    /** Deletes a file. Does nothing if it does not exist. */
    public function delete(string $filename): void;

    public function exists(string $filename): bool;
}
