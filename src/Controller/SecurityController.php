<?php

namespace App\Controller;

use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\Routing\Attribute\Route;

class SecurityController extends AbstractController
{
    #[Route('/auth', name: 'app_login_jwt', methods: ['POST'])]
    public function jwtLogin(): Response
    {
    }
}
