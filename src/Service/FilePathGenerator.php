<?php

namespace App\Service;

/**
 * Converts a file's display name into the slug-style filename stored in File::$path.
 * The result is a bare filename, never a path — FileStorageService resolves it
 * against the configured storage root.
 */
class FilePathGenerator
{
    /** Bytes of randomness drawn for the uniqueness suffix. */
    private const SUFFIX_RANDOM_BYTES = 8;

    /** Hex characters of that randomness actually kept in the filename. */
    private const SUFFIX_LENGTH = 12;

    public function generate(string $title, string $extension = 'ink'): string
    {
        // 1. Convert to lowercase.
        $filename = strtolower($title);

        // 2. Replace non-alphanumeric characters (except spaces and dashes) with nothing.
        // This removes parentheses, special symbols, etc.
        $filename = preg_replace('/[^\w\s-]/', '', $filename);

        // 3. Replace all spaces and underscores with a single dash.
        $filename = preg_replace('/[\s_]+/', '-', $filename);

        // 4. Collapse multiple dashes into a single dash.
        $filename = preg_replace('/-+/', '-', $filename);

        // 5. Trim any leading or trailing dashes (to enforce no dash at start/end).
        $filename = trim($filename, '-');

        // 6. Ensure the filename is not empty (e.g., if input was just symbols).
        if (empty($filename)) {
            $filename = 'default-file';
        }

        // 7. Append a unique suffix so two files with the same title never share a path.
        $filename .= '-' . substr(bin2hex(random_bytes(self::SUFFIX_RANDOM_BYTES)), 0, self::SUFFIX_LENGTH);

        // 8. Append the extension.
        $filename .= '.' . strtolower($extension);

        // Check that the result has the expected form before it reaches the
        // filesystem: lowercase alphanumeric groups separated by single dashes,
        // then the extension. Anything else means a step above is broken.
        $ext = preg_quote(strtolower($extension), '/');
        if (preg_match('/^[a-z0-9]+(-[a-z0-9]+)*\.' . $ext . '$/', $filename) !== 1) {
            throw new \RuntimeException(
                sprintf('FilePathGenerator produced an invalid filename: "%s"', $filename)
            );
        }

        return $filename;
    }
}
