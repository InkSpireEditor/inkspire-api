<?php

namespace App\Tests;

use App\Entity\User;
use Doctrine\ORM\EntityManagerInterface;
use Symfony\Bundle\FrameworkBundle\KernelBrowser;
use Symfony\Bundle\FrameworkBundle\Test\WebTestCase;
use Symfony\Component\PasswordHasher\Hasher\UserPasswordHasherInterface;

class LoginControllerTest extends WebTestCase
{
    private KernelBrowser $client;
    private ?EntityManagerInterface $entityManager;

    private $email = 'test@example.com';
    private $password = 'password';

    protected function setUp(): void
    {
        $this->client = static::createClient();
        $container = static::getContainer();
        $this->entityManager = $container->get('doctrine.orm.entity_manager');

        // Clean up the database before each test
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();

        /** @var UserPasswordHasherInterface $passwordHasher */
        $passwordHasher = $container->get('security.user_password_hasher');

        $user = (new User())->setEmail($this->email);
        $user->setPassword($passwordHasher->hashPassword($user, $this->password));

        $this->entityManager->persist($user);
        $this->entityManager->flush();
    }

    public function testLogin(): void
    {
        // Denied - Can't login with invalid email address.
        $this->client->jsonRequest('POST', '/auth', [
            'username' => 'doesNotExist@example.com',
            'password' => 'password',
        ]);
        $this->assertResponseStatusCodeSame(401);
        $data = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('message', $data);

        // Denied - Can't login with invalid password.
        $this->client->jsonRequest('POST', '/auth', [
            'username' => $this->email,
            'password' => 'bad-password',
        ]);
        $this->assertResponseStatusCodeSame(401);
        $data = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('message', $data);

        // Success - Login with valid credentials is allowed.
        $this->client->jsonRequest('POST', '/auth', [
            'username' => $this->email,
            'password' => $this->password,
        ]);
        $this->assertResponseIsSuccessful();
        $data = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('token', $data);
    }

    protected function tearDown(): void
    {
        parent::tearDown();

        // Clean up the database after each test
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();

        // doing this is recommended to avoid memory leaks
        $this->entityManager->close();
        $this->entityManager = null;
    }
}
