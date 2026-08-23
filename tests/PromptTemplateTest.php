<?php

namespace App\Tests;

use Symfony\Bundle\FrameworkBundle\Test\KernelTestCase;
use Twig\Environment;

/**
 * The prompt is assembled by Twig, whose default escaping is meant for HTML
 * output. Prose is not HTML: escaping it corrupts the text the model is asked
 * to continue. These tests pin that down.
 */
class PromptTemplateTest extends KernelTestCase
{
    private Environment $twig;

    protected function setUp(): void
    {
        parent::setUp();
        self::bootKernel();
        $this->twig = static::getContainer()->get(Environment::class);
    }

    private function render(string $prompt): string
    {
        return $this->twig->render('prompt.twig', ['prompt' => $prompt]);
    }

    public function test_01_writerTextIsNotHtmlEscaped(): void
    {
        $text = 'She said "it\'s fine" & left.';
        $rendered = $this->render($text);

        $this->assertStringContainsString($text, $rendered);
        $this->assertStringNotContainsString('&quot;', $rendered);
        $this->assertStringNotContainsString('&#039;', $rendered);
        $this->assertStringNotContainsString('&amp;', $rendered);
    }

    public function test_02_promptEndsOnTheWritersLastCharacter(): void
    {
        // The model's output is concatenated straight onto the writer's text, so
        // a trailing newline here would invite it to start a fresh line instead
        // of continuing the sentence in progress.
        $rendered = $this->render('The road went on');

        $this->assertStringEndsWith('The road went on', $rendered);
    }

    public function test_03_markdownIsPreservedVerbatim(): void
    {
        $text = "# Chapter One\n\nIt was *cold*, and the `wind` cut deep >";
        $this->assertStringContainsString($text, $this->render($text));
    }

    public function test_04_instructsAgainstRepeatingTheExistingText(): void
    {
        $rendered = $this->render('anything');

        $this->assertMatchesRegularExpression('/never repeat/i', $rendered);
        $this->assertMatchesRegularExpression('/punctuation/i', $rendered);
    }

    public function test_05_forbidsHtmlOutput(): void
    {
        // A model asked for a line break will reach for <br> unless told not to;
        // the editor is a plain textarea, so the tag would land in the prose.
        $rendered = $this->render('anything');

        $this->assertMatchesRegularExpression('/never emit html/i', $rendered);
        $this->assertMatchesRegularExpression('/newline character/i', $rendered);
    }
}
