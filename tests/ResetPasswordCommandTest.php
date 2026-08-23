<?php

namespace App\Tests;

use App\Entity\RefreshToken;
use App\Entity\User;
use Doctrine\ORM\EntityManagerInterface;
use Symfony\Bundle\FrameworkBundle\Console\Application;
use Symfony\Bundle\FrameworkBundle\Test\KernelTestCase;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Tester\CommandTester;
use Symfony\Component\PasswordHasher\Hasher\UserPasswordHasherInterface;

class ResetPasswordCommandTest extends KernelTestCase
{
    private EntityManagerInterface $entityManager;
    private UserPasswordHasherInterface $passwordHasher;
    private CommandTester $commandTester;

    private string $email = 'reset-target@example.com';
    private string $originalPassword = 'original-password';

    protected function setUp(): void
    {
        parent::setUp();
        $kernel = self::bootKernel();
        $container = static::getContainer();

        $this->entityManager = $container->get('doctrine.orm.entity_manager');
        $this->passwordHasher = $container->get('security.user_password_hasher');

        $this->entityManager->createQuery('DELETE FROM App\\Entity\\RefreshToken')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();

        $user = (new User())->setEmail($this->email);
        $user->setPassword($this->passwordHasher->hashPassword($user, $this->originalPassword));
        $this->entityManager->persist($user);
        $this->entityManager->flush();

        $application = new Application($kernel);
        $this->commandTester = new CommandTester($application->find('app:user:reset-password'));
    }

    protected function tearDown(): void
    {
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\RefreshToken')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();
        parent::tearDown();
    }

    private function refreshUser(): User
    {
        $this->entityManager->clear();

        return $this->entityManager->getRepository(User::class)->findOneBy(['email' => $this->email]);
    }

    private function giveUserRefreshToken(): void
    {
        $user = $this->entityManager->getRepository(User::class)->findOneBy(['email' => $this->email]);
        $token = (new RefreshToken())
            ->setToken(bin2hex(random_bytes(RefreshToken::TOKEN_BYTES)))
            ->setUser($user)
            ->setExpiresAt(new \DateTimeImmutable('+7 days'));
        $this->entityManager->persist($token);
        $this->entityManager->flush();
    }

    public function test_01_setsExplicitPassword(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'brand-new-password',
        ]);

        $this->assertSame(Command::SUCCESS, $exitCode);

        $user = $this->refreshUser();
        $this->assertTrue($this->passwordHasher->isPasswordValid($user, 'brand-new-password'));
        $this->assertFalse($this->passwordHasher->isPasswordValid($user, $this->originalPassword));
    }

    public function test_02_generatesAndPrintsPasswordWhenOmitted(): void
    {
        $exitCode = $this->commandTester->execute(['email' => $this->email]);

        $this->assertSame(Command::SUCCESS, $exitCode);

        $output = $this->commandTester->getDisplay();
        $this->assertMatchesRegularExpression('/Generated password: [0-9a-f]{24}/', $output);

        preg_match('/Generated password: ([0-9a-f]{24})/', $output, $matches);
        $this->assertTrue($this->passwordHasher->isPasswordValid($this->refreshUser(), $matches[1]));
    }

    public function test_03_revokesRefreshTokensByDefault(): void
    {
        $this->giveUserRefreshToken();
        $this->assertCount(1, $this->entityManager->getRepository(RefreshToken::class)->findAll());

        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'brand-new-password',
        ]);

        $this->assertSame(Command::SUCCESS, $exitCode);
        $this->entityManager->clear();
        $this->assertCount(0, $this->entityManager->getRepository(RefreshToken::class)->findAll());
        $this->assertStringContainsString('Refresh tokens revoked: 1', $this->commandTester->getDisplay());
    }

    public function test_04_keepSessionsLeavesRefreshTokensIntact(): void
    {
        $this->giveUserRefreshToken();

        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'brand-new-password',
            '--keep-sessions' => true,
        ]);

        $this->assertSame(Command::SUCCESS, $exitCode);
        $this->entityManager->clear();
        $this->assertCount(1, $this->entityManager->getRepository(RefreshToken::class)->findAll());
    }

    public function test_05_failsOnUnknownEmail(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => 'nobody@example.com',
            '--password' => 'brand-new-password',
        ]);

        $this->assertSame(Command::FAILURE, $exitCode);
        $this->assertStringContainsString('No account found', $this->commandTester->getDisplay());
    }

    public function test_06_rejectsTooShortPassword(): void
    {
        $exitCode = $this->commandTester->execute([
            'email' => $this->email,
            '--password' => 'abc',
        ]);

        $this->assertSame(Command::FAILURE, $exitCode);
        $this->assertStringContainsString('at least 6 characters', $this->commandTester->getDisplay());

        // The stored password must be untouched after a rejected reset.
        $this->assertTrue($this->passwordHasher->isPasswordValid($this->refreshUser(), $this->originalPassword));
    }
}
