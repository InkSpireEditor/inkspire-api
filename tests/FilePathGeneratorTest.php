<?php

namespace App\Tests;

use App\Service\FilePathGenerator;
use PHPUnit\Framework\TestCase;

class FilePathGeneratorTest extends TestCase
{
    private FilePathGenerator $generator;

    protected function setUp(): void
    {
        $this->generator = new FilePathGenerator();
    }

    private function assertPath(string $expectedSlug, string $filename, string $ext = 'ink'): void
    {
        $this->assertMatchesRegularExpression(
            '/^' . preg_quote($expectedSlug, '/') . '-[0-9a-f]{12}\.' . $ext . '$/',
            $filename
        );
    }

    public function test_simpleTitle(): void
    {
        $this->assertPath('my-novel', $this->generator->generate('My Novel'));
    }

    public function test_titleWithDigits(): void
    {
        $this->assertPath('chapter-1', $this->generator->generate('Chapter 1'));
    }

    public function test_titleWithDigitsInMiddle(): void
    {
        $this->assertPath('chapter-1-of-3', $this->generator->generate('Chapter 1 Of 3'));
    }

    public function test_titleStartingWithDigit(): void
    {
        $this->assertPath('1984', $this->generator->generate('1984'));
    }

    public function test_titleWithDuplicateSuffix(): void
    {
        // "(1)" suffix appended by the uniqueness loop in the controller
        $this->assertPath('duplicate-name-1', $this->generator->generate('Duplicate Name (1)'));
    }

    public function test_titleWithSpecialCharacters(): void
    {
        $this->assertPath('hello-world-beyond', $this->generator->generate('Hello, World! & Beyond'));
    }

    public function test_titleWithExtraSpacesAndDashes(): void
    {
        $this->assertPath('my-file', $this->generator->generate('  my  --  file  '));
    }

    public function test_titleWithUnderscores(): void
    {
        $this->assertPath('my-file-name', $this->generator->generate('my_file_name'));
    }

    public function test_titleAllSymbolsFallsBackToDefault(): void
    {
        $path = $this->generator->generate('!@#$%^&*()');
        $this->assertPath('default-file', $path);
        $this->assertMatchesRegularExpression('/^[a-z0-9]+(-[a-z0-9]+)*\.ink$/', basename($path));
    }

    public function test_titleWithOnlyDashes(): void
    {
        $path = $this->generator->generate('---');
        $this->assertPath('default-file', $path);
        $this->assertMatchesRegularExpression('/^[a-z0-9]+(-[a-z0-9]+)*\.ink$/', basename($path));
    }

    public function test_uppercaseTitle(): void
    {
        $this->assertPath('my-great-novel', $this->generator->generate('MY GREAT NOVEL'));
    }

    public function test_customExtension(): void
    {
        $this->assertPath('my-draft', $this->generator->generate('My Draft', 'md'), 'md');
    }

    public function test_generatedFilenameMatchesPattern(): void
    {
        $titles = ['Simple', 'With Numbers 42', 'Chapter 1 Of 3', 'Duplicate (1)', 'Mixed 1 And 2 Words'];
        foreach ($titles as $title) {
            $filename = basename($this->generator->generate($title));
            $this->assertMatchesRegularExpression(
                '/^[a-z0-9]+(-[a-z0-9]+)*\.ink$/',
                $filename,
                "Failed for title: \"$title\""
            );
        }
    }

    public function test_uniquenessAcrossCalls(): void
    {
        $path1 = $this->generator->generate('Same Title');
        $path2 = $this->generator->generate('Same Title');
        $this->assertNotSame($path1, $path2);
    }
}
