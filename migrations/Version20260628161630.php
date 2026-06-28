<?php

declare(strict_types=1);

namespace DoctrineMigrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

/**
 * Strips the absolute path prefix from File.path, storing only the filename.
 * SQLite lacks REVERSE(), so the transformation is done in PHP via postUp().
 */
final class Version20260628161630 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Store relative filename in File.path instead of absolute path (A-02)';
    }

    public function up(Schema $schema): void
    {
        // Data migration handled in postUp() — no DDL changes required.
    }

    public function postUp(Schema $schema): void
    {
        $rows = $this->connection->fetchAllAssociative(
            'SELECT id, path FROM "file" WHERE path LIKE "/%"'
        );
        foreach ($rows as $row) {
            $this->connection->executeStatement(
                'UPDATE "file" SET path = :path WHERE id = :id',
                ['path' => basename($row['path']), 'id' => $row['id']]
            );
        }
    }

    public function down(Schema $schema): void
    {
        // Cannot restore the absolute prefix without knowing the original deployment path.
        $this->abortIf(true, 'A-02 down migration is not supported: original absolute prefix is environment-specific.');
    }
}
