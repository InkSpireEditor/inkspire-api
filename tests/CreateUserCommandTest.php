<?php

namespace App\Tests;

use App\Entity\User;
use Doctrine\ORM\EntityManagerInterface;
use Symfony\Bundle\FrameworkBundle\Console\Application;
use Symfony\Bundle\FrameworkBundle\KernelBrowser;
use Symfony\Bundle\FrameworkBundle\Test\WebTestCase;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Tester\CommandTester;
use Symfony\Component\PasswordHasher\Hasher\UserPasswordHasherInterface;

class CreateUserCommandTest extends WebTestCase
{
    private EntityManagerInterface $entityManager;
    private UserPasswordHasherInterface $passwordHasher;
    private CommandTester $commandTester;
    private KernelBrowser $client;

    private string $email = 'new-user@example.com';

    protected function setUp(): void
    {
        parent::setUp();
        // WebTestCase rather than KernelTestCase: test_08 logs in over HTTP to
        // prove the created account works against the real firewall.
        $this->client = static::createClient();
        $container = static::getContainer();

        $this->entityManager = $container->get('doctrine.orm.entity_manager');
        $this->passwordHasher = $container->get('security.user_password_hasher');

        $this->entityManager->createQuery('DELETE FROM App\\Entity\\RefreshToken')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();

        $application = new Application(static::$kernel);
        $this->commandTester = new CommandTester($application->find('app:user:create'));
    }

    protected function tearDown(): void
    {
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\RefreshToken')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();
        parent::tearDown();
    }

    private function findUser(?string $email = null): ?User
    {
        $this->entityManager->clear();

        return $this->entityManager->getRepository(User::class)
            ->findOneBy(['email' => $email ?? $this->email]);
    }

    public function test_01_createsUserWithExplicitPassword(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'a-good-password',
        ]);

        $this->assertSame(Command::SUCCESS, $exitCode);

        $user = $this->findUser();
        $this->assertNotNull($user);
        $this->assertTrue($this->passwordHasher->isPasswordValid($user, 'a-good-password'));
        $this->assertSame(['ROLE_USER'], $user->getRoles());
    }

    public function test_02_generatesAndPrintsPasswordWhenOmitted(): void
    {
        $exitCode = $this->commandTester->execute(['email' => $this->email]);

        $this->assertSame(Command::SUCCESS, $exitCode);

        $output = $this->commandTester->getDisplay();
        $this->assertMatchesRegularExpression('/Generated password: [0-9a-f]{24}/', $output);

        preg_match('/Generated password: ([0-9a-f]{24})/', $output, $matches);
        $this->assertTrue($this->passwordHasher->isPasswordValid($this->findUser(), $matches[1]));
    }

    public function test_03_grantsRequestedRoles(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'a-good-password',
            '--role' => ['ROLE_ADMIN'],
        ]);

        $this->assertSame(Command::SUCCESS, $exitCode);
        $this->assertContains('ROLE_ADMIN', $this->findUser()->getRoles());
    }

    public function test_04_rejectsDuplicateEmail(): void
    {
        $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'a-good-password',
        ]);

        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'another-password',
        ]);

        $this->assertSame(Command::FAILURE, $exitCode);
        $this->assertStringContainsString('already exists', $this->commandTester->getDisplay());

        // The original account must be untouched by the rejected second run.
        $this->assertTrue($this->passwordHasher->isPasswordValid($this->findUser(), 'a-good-password'));
    }

    public function test_05_rejectsInvalidEmail(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => 'not-an-email',
            '--password' => 'a-good-password',
        ]);

        $this->assertSame(Command::FAILURE, $exitCode);
        $this->assertStringContainsString('not a valid email address', $this->commandTester->getDisplay());
        $this->assertNull($this->findUser('not-an-email'));
    }

    public function test_06_rejectsTooShortPassword(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'abc',
        ]);

        $this->assertSame(Command::FAILURE, $exitCode);
        $this->assertStringContainsString('at least 6 characters', $this->commandTester->getDisplay());
        $this->assertNull($this->findUser());
    }

    public function test_07_rejectsMalformedRole(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'a-good-password',
            '--role' => ['admin'],
        ]);

        $this->assertSame(Command::FAILURE, $exitCode);
        $this->assertStringContainsString('must start with ROLE_', $this->commandTester->getDisplay());
        $this->assertNull($this->findUser());
    }

    public function test_08_createdUserCanAuthenticate(): void
    {
        $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'a-good-password',
        ]);

        // The point of the command: the account works against the real login
        // endpoint, not just against the hasher.
        $this->client->jsonRequest('POST', '/auth', [
            'username' => $this->email,
            'password' => 'a-good-password',
        ]);

        $this->assertResponseIsSuccessful();
        $this->assertArrayHasKey('token', json_decode($this->client->getResponse()->getContent(), true));
    }
}
